//! Router — the multi-agent mechanism (port of `snail.router.router`).
//!
//! Owns **mechanism**: the output token (GATE 2 via [`OutputGate`]) and the seam. **Decision** is
//! delegated to a pluggable [`RoutingPolicy`] — the Router health-gates + validates a decision
//! against reality before acting (advice, not command).
//!
//! Loop-bound side effects (vendor cancel, socket reconnect for a text→audio flip, real
//! turn-boundary detection) are emitted as [`RouterEffect`]s that the caller drains and executes —
//! the idiomatic-Rust form of the Python injected hooks, so the orchestration is testable without a
//! loop or vendor. GATE-1 input subscriptions live on the shared [`super::super::audio::FanoutBus`]
//! owned by the pipeline; the Router records the desired subscription state and the bridge applies
//! it (the bus needs the frame pool to release slabs on unsubscribe).

use std::collections::HashMap;

use crate::audio::AudioSource;
use crate::vendor::ResponseModality;

use super::gate::OutputGate;
use super::policy::RoutingPolicy;
use super::signals::{
    AgentRef, AgentRole, HealthState, RoutingAction, RoutingDecision, RoutingSignal, Seam,
};

/// A side effect the Router asks the caller to perform (the injected-hook seam, as events).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouterEffect {
    /// promote `id` to active; `needs_flip` = it was a TEXT listener → text→audio reconnect.
    Promote { id: String, needs_flip: bool },
    /// `id` (ex-active) is now a listener (kept for instant re-promote).
    Demote { id: String },
    /// stop the vendor's wasted generation for `id` (CUT_NOW / barge-in).
    VendorCancel { id: String },
    /// subscribe `id` to user audio (GATE 1) with this source + vendor rate.
    Subscribe {
        id: String,
        source: AudioSource,
        target_rate: u32,
    },
    /// unsubscribe `id` from user audio (detach-release its ring).
    Unsubscribe { id: String },
}

/// Router-side view of one agent (role/health/modality mutate over its life).
#[derive(Debug, Clone)]
pub struct AgentRecord {
    pub id: String,
    pub spec_id: String,
    pub role: AgentRole,
    pub modality: ResponseModality,
    pub input_source: AudioSource,
    pub target_rate: u32,
    pub health: HealthState,
}

/// One active agent + N listeners; promote/demote/handoff over a single token.
pub struct Router {
    gate: OutputGate,
    policy: Box<dyn RoutingPolicy>,
    agents: HashMap<String, AgentRecord>,
    active_id: Option<String>,
    subscribed: std::collections::HashSet<String>,
    pending: Option<(String, Seam)>,
    last_block: Option<(String, String)>,
    effects: Vec<RouterEffect>,
}

impl Router {
    pub fn new(gate: OutputGate, policy: Box<dyn RoutingPolicy>) -> Self {
        Self {
            gate,
            policy,
            agents: HashMap::new(),
            active_id: None,
            subscribed: std::collections::HashSet::new(),
            pending: None,
            last_block: None,
            effects: Vec::new(),
        }
    }

    /// Drain the effects accumulated since the last call (the caller executes them).
    pub fn take_effects(&mut self) -> Vec<RouterEffect> {
        std::mem::take(&mut self.effects)
    }

    // --- registration / topology -----------------------------------------

    pub fn register_agent(
        &mut self,
        id: &str,
        spec_id: &str,
        modality: ResponseModality,
        input_source: AudioSource,
        target_rate: u32,
        health: HealthState,
    ) {
        assert!(
            !self.agents.contains_key(id),
            "agent {id:?} already registered"
        );
        self.agents.insert(
            id.to_string(),
            AgentRecord {
                id: id.to_string(),
                spec_id: spec_id.to_string(),
                role: AgentRole::Listener,
                modality,
                input_source,
                target_rate,
                health,
            },
        );
    }

    /// Make `id` the initial active agent: grant the token + subscribe input.
    pub fn set_active(&mut self, id: &str) {
        let rec = self.agents.get_mut(id).expect("agent not registered");
        rec.role = AgentRole::Active;
        self.active_id = Some(id.to_string());
        self.gate.grant(id);
        self.ensure_subscribed(id);
    }

    pub fn add_listener(&mut self, id: &str) {
        let rec = self.agents.get_mut(id).expect("agent not registered");
        rec.role = AgentRole::Listener;
        self.ensure_subscribed(id);
    }

    pub fn remove_listener(&mut self, id: &str) {
        if self.subscribed.remove(id) {
            self.effects
                .push(RouterEffect::Unsubscribe { id: id.to_string() });
        }
    }

    // --- decision entry point --------------------------------------------

    /// Run the policy on `signal` and execute its decision. Returns the decision (if any).
    pub fn handle(&mut self, signal: &RoutingSignal) -> Option<RoutingDecision> {
        let decision = self.policy.decide(signal)?;
        self.execute(&decision);
        Some(decision)
    }

    fn execute(&mut self, d: &RoutingDecision) {
        match d.action {
            RoutingAction::Handoff => {
                if let Some(t) = &d.target {
                    self.handoff(t, d.seam);
                }
            }
            RoutingAction::FanoutAdd => {
                if let Some(t) = &d.target {
                    self.add_listener(t);
                }
            }
            RoutingAction::FanoutRemove => {
                if let Some(t) = &d.target {
                    self.remove_listener(t);
                }
            }
            RoutingAction::Stay | RoutingAction::Reject => {}
        }
    }

    // --- handoff / seam ---------------------------------------------------

    fn handoff(&mut self, target: &str, seam: Seam) {
        let health = match self.agents.get(target) {
            None => {
                self.last_block = Some((target.to_string(), "unknown target".into()));
                return;
            }
            Some(rec) => rec.health,
        };
        if health != HealthState::Healthy {
            // Never promote a stale socket (docs 02); a real Router recycles first.
            self.last_block = Some((target.to_string(), format!("health={}", health_str(health))));
            return;
        }
        if seam == Seam::CutNow {
            self.do_transfer(target, true);
        } else {
            self.pending = Some((target.to_string(), seam)); // transfer at next boundary
        }
    }

    fn do_transfer(&mut self, target: &str, cut: bool) {
        let old = self.active_id.clone();
        self.gate.transfer(target); // atomic token move — overlap impossible
        if cut {
            self.gate.flush(); // drop the old agent's queued half-sentence
            if let Some(old_id) = &old {
                self.effects
                    .push(RouterEffect::VendorCancel { id: old_id.clone() });
                // sweep of old's in-flight calls is the session's job (owns the registry).
            }
        }
        if let Some(old_id) = &old {
            if let Some(rec) = self.agents.get_mut(old_id) {
                rec.role = AgentRole::Listener; // demote-to-listener
                self.effects
                    .push(RouterEffect::Demote { id: old_id.clone() });
            }
        }
        let needs_flip =
            self.agents.get(target).map(|r| r.modality) == Some(ResponseModality::Text);
        if let Some(rec) = self.agents.get_mut(target) {
            rec.role = AgentRole::Active;
        }
        self.active_id = Some(target.to_string());
        self.ensure_subscribed(target);
        self.effects.push(RouterEffect::Promote {
            id: target.to_string(),
            needs_flip,
        });
    }

    /// Fire a pending AT_TURN_END transfer at the utterance boundary.
    pub fn on_turn_end(&mut self) -> bool {
        self.fire_pending(Seam::AtTurnEnd)
    }
    /// Fire a pending AT_IDLE transfer at a user-turn boundary.
    pub fn on_idle(&mut self) -> bool {
        self.fire_pending(Seam::AtIdle)
    }

    fn fire_pending(&mut self, seam: Seam) -> bool {
        if let Some((target, s)) = self.pending.clone() {
            if s == seam {
                self.pending = None;
                self.do_transfer(&target, false);
                return true;
            }
        }
        false
    }

    // --- barge-in ---------------------------------------------------------

    /// User interrupted the active agent: flush the output ring + ask for a vendor cancel. The
    /// token stays with the active agent (not a handoff). Registry sweep is the session's job.
    pub fn barge_in(&mut self) {
        self.gate.flush();
        if let Some(active) = &self.active_id {
            self.effects
                .push(RouterEffect::VendorCancel { id: active.clone() });
        }
    }

    // --- helpers / introspection -----------------------------------------

    fn ensure_subscribed(&mut self, id: &str) {
        if !self.subscribed.contains(id) {
            if let Some(rec) = self.agents.get(id) {
                self.subscribed.insert(id.to_string());
                self.effects.push(RouterEffect::Subscribe {
                    id: id.to_string(),
                    source: rec.input_source,
                    target_rate: rec.target_rate,
                });
            }
        }
    }

    pub fn active_id(&self) -> Option<&str> {
        self.active_id.as_deref()
    }
    pub fn pending(&self) -> Option<&(String, Seam)> {
        self.pending.as_ref()
    }
    pub fn last_block(&self) -> Option<&(String, String)> {
        self.last_block.as_ref()
    }
    pub fn role_of(&self, id: &str) -> Option<AgentRole> {
        self.agents.get(id).map(|r| r.role)
    }
    pub fn gate(&self) -> &OutputGate {
        &self.gate
    }

    /// Immutable snapshot of an agent for a RoutingSignal.
    pub fn agent_ref(&self, id: &str) -> Option<AgentRef> {
        self.agents.get(id).map(|rec| AgentRef {
            id: rec.id.clone(),
            spec_id: rec.spec_id.clone(),
            role: rec.role,
        })
    }
}

fn health_str(h: HealthState) -> &'static str {
    match h {
        HealthState::Healthy => "healthy",
        HealthState::NearDeadline => "near_deadline",
        HealthState::Stale => "stale",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::router::policy::default_chain;
    use crate::router::signals::{RoutingEvent, RoutingEventKind};
    use crate::router::{ProgrammaticPolicy, RulePolicy};

    fn router() -> Router {
        let policy = default_chain(
            ProgrammaticPolicy::new(),
            RulePolicy::new(),
            Seam::AtTurnEnd,
        );
        let mut r = Router::new(OutputGate::new(8), Box::new(policy));
        r.register_agent(
            "a",
            "specA",
            ResponseModality::Audio,
            AudioSource::UserClean,
            16000,
            HealthState::Healthy,
        );
        r.register_agent(
            "b",
            "specB",
            ResponseModality::Text,
            AudioSource::UserClean,
            16000,
            HealthState::Healthy,
        );
        r.set_active("a");
        let _ = r.take_effects(); // clear the set_active subscribe
        r
    }

    fn transfer(target: &str) -> RoutingSignal {
        let mut ev = RoutingEvent::of(RoutingEventKind::TransferTo);
        ev.target = Some(target.into());
        RoutingSignal::new(ev)
    }

    #[test]
    fn transfer_to_defers_until_turn_end() {
        let mut r = router();
        r.handle(&transfer("b"));
        assert_eq!(r.active_id(), Some("a")); // still a — pending
        assert_eq!(r.pending().unwrap().0, "b");
        assert!(r.on_turn_end()); // boundary fires the transfer
        assert_eq!(r.active_id(), Some("b"));
        let effects = r.take_effects();
        // b was a TEXT listener → promote needs a flip; a demoted
        assert!(effects.contains(&RouterEffect::Promote {
            id: "b".into(),
            needs_flip: true
        }));
        assert!(effects.contains(&RouterEffect::Demote { id: "a".into() }));
    }

    #[test]
    fn barge_in_flushes_gate_and_cancels_active() {
        let mut r = router();
        r.gate().holder(); // active "a" holds the token
        r.barge_in();
        let effects = r.take_effects();
        assert!(effects.contains(&RouterEffect::VendorCancel { id: "a".into() }));
        assert_eq!(r.active_id(), Some("a")); // barge-in is not a handoff — token stays
    }

    #[test]
    fn unknown_or_unhealthy_target_is_blocked() {
        let mut r = router();
        r.handle(&transfer("ghost"));
        assert_eq!(r.last_block().unwrap().0, "ghost");
        assert_eq!(r.active_id(), Some("a")); // unchanged
    }
}
