//! RoutingPolicy + built-in policies (port of `snail.router.policy`).
//!
//! Mechanism (Router) vs decision (policy) split: a policy never touches sockets; it consumes a
//! [`RoutingSignal`] and returns advice. `decide` runs only on real events; `None` = "keep current
//! routing" (the cheap 99%). Precedence is not hardcoded — it is the order of a [`ChainPolicy`].
//! The shipped default puts Programmatic first so an explicit app decision beats the model's
//! `transfer_to`.

use std::collections::VecDeque;

use super::predicate::Predicate;
use super::signals::{RoutingAction, RoutingDecision, RoutingEventKind, RoutingSignal, Seam};

/// A routing policy: advice for this signal, or `None` to keep current routing. Takes `&mut self`
/// so stateful policies (e.g. a programmatic queue) can mutate on decide.
pub trait RoutingPolicy {
    fn decide(&mut self, signal: &RoutingSignal) -> Option<RoutingDecision>;
}

/// Active agent emitted `transfer_to` → HANDOFF(target). Free, deterministic.
pub struct ControlToolPolicy {
    seam: Seam,
}

impl ControlToolPolicy {
    pub fn new(seam: Seam) -> Self {
        Self { seam }
    }
}

impl RoutingPolicy for ControlToolPolicy {
    fn decide(&mut self, signal: &RoutingSignal) -> Option<RoutingDecision> {
        let ev = &signal.event;
        if ev.kind == Some(RoutingEventKind::TransferTo) {
            if let Some(target) = &ev.target {
                if !target.is_empty() {
                    return Some(RoutingDecision {
                        action: RoutingAction::Handoff,
                        target: Some(target.clone()),
                        seam: self.seam,
                        reason: "control tool transfer_to".into(),
                        confidence: None,
                    });
                }
            }
        }
        None
    }
}

/// App/backend pushes a decision from outside (button, backend event). FIFO; `decide` returns the
/// oldest queued one on any signal and clears it. Placed first in the default chain so it wins.
#[derive(Default)]
pub struct ProgrammaticPolicy {
    queue: VecDeque<RoutingDecision>,
}

impl ProgrammaticPolicy {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn push(&mut self, decision: RoutingDecision) {
        self.queue.push_back(decision);
    }
}

impl RoutingPolicy for ProgrammaticPolicy {
    fn decide(&mut self, _signal: &RoutingSignal) -> Option<RoutingDecision> {
        self.queue.pop_front()
    }
}

/// `when` predicate → `then` decision template.
pub struct Rule {
    pub when: Predicate,
    pub then: RoutingDecision,
}

/// Ordered rules; first matching predicate wins (local precedence).
#[derive(Default)]
pub struct RulePolicy {
    rules: Vec<Rule>,
}

impl RulePolicy {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn add(mut self, rule: Rule) -> Self {
        self.rules.push(rule);
        self
    }
}

impl RoutingPolicy for RulePolicy {
    fn decide(&mut self, signal: &RoutingSignal) -> Option<RoutingDecision> {
        let value = signal.to_value();
        self.rules
            .iter()
            .find(|r| r.when.matches(&value))
            .map(|r| r.then.clone())
    }
}

/// Ordered composite; first non-`None` wins. **Order = precedence.**
pub struct ChainPolicy {
    policies: Vec<Box<dyn RoutingPolicy>>,
}

impl ChainPolicy {
    pub fn new(policies: Vec<Box<dyn RoutingPolicy>>) -> Self {
        Self { policies }
    }
}

impl RoutingPolicy for ChainPolicy {
    fn decide(&mut self, signal: &RoutingSignal) -> Option<RoutingDecision> {
        for policy in self.policies.iter_mut() {
            if let Some(decision) = policy.decide(signal) {
                return Some(decision);
            }
        }
        None
    }
}

/// The shipped default: Programmatic → ControlTool → Rule. Programmatic first so an explicit app
/// decision beats the model's `transfer_to`; rules last as the catch-all.
pub fn default_chain(
    programmatic: ProgrammaticPolicy,
    rules: RulePolicy,
    control_seam: Seam,
) -> ChainPolicy {
    ChainPolicy::new(vec![
        Box::new(programmatic),
        Box::new(ControlToolPolicy::new(control_seam)),
        Box::new(rules),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::router::predicate::f;
    use crate::router::signals::RoutingEvent;
    use serde_json::json;

    fn transfer_signal(target: &str) -> RoutingSignal {
        let mut ev = RoutingEvent::of(RoutingEventKind::TransferTo);
        ev.target = Some(target.into());
        RoutingSignal::new(ev)
    }

    #[test]
    fn control_tool_emits_handoff() {
        let mut p = ControlToolPolicy::new(Seam::AtTurnEnd);
        let d = p.decide(&transfer_signal("billing")).unwrap();
        assert_eq!(d.action, RoutingAction::Handoff);
        assert_eq!(d.target.as_deref(), Some("billing"));
    }

    #[test]
    fn programmatic_is_fifo_and_wins_in_chain() {
        let mut prog = ProgrammaticPolicy::new();
        prog.push(RoutingDecision {
            target: Some("human".into()),
            reason: "button".into(),
            ..RoutingDecision::new(RoutingAction::Handoff)
        });
        // programmatic first → beats the control-tool transfer_to in the same signal
        let mut chain = default_chain(prog, RulePolicy::new(), Seam::AtTurnEnd);
        let d = chain.decide(&transfer_signal("billing")).unwrap();
        assert_eq!(d.target.as_deref(), Some("human")); // programmatic won
                                                        // queue now empty → control tool wins
        let d2 = chain.decide(&transfer_signal("billing")).unwrap();
        assert_eq!(d2.target.as_deref(), Some("billing"));
    }

    #[test]
    fn rule_matches_predicate() {
        let mut rules = RulePolicy::new().add(Rule {
            when: f("event.status").eq(json!("escalate")),
            then: RoutingDecision {
                target: Some("supervisor".into()),
                ..RoutingDecision::new(RoutingAction::Handoff)
            },
        });
        let mut ev = RoutingEvent::of(RoutingEventKind::ToolResult);
        ev.status = Some("escalate".into());
        let d = rules.decide(&RoutingSignal::new(ev)).unwrap();
        assert_eq!(d.target.as_deref(), Some("supervisor"));
    }

    #[test]
    fn no_opinion_returns_none() {
        let mut rules = RulePolicy::new();
        assert!(rules.decide(&transfer_signal("x")).is_none());
    }
}
