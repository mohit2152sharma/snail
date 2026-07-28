//! RulePolicy predicate surface — declarative default (port of `snail.router.predicate`).
//!
//! A predicate reads only the [`super::RoutingSignal`] (as a JSON value — no sockets, no live
//! vendor state). The declarative form is a serializable `{field, op, value}` tree — safe,
//! inspectable, no eval. Text matches are a coarse intent hint only — never gate authority on one;
//! that belongs in a deterministic `tool_result.status`.

use regex::Regex;
use serde_json::Value;

/// Walk a dotted `path` (e.g. `"event.status"`) against a JSON signal; missing link → `Null`.
pub fn resolve_field<'a>(signal: &'a Value, path: &str) -> &'a Value {
    let mut obj = signal;
    for part in path.split('.') {
        match obj {
            Value::Object(map) => match map.get(part) {
                Some(v) => obj = v,
                None => return &Value::Null,
            },
            _ => return &Value::Null,
        }
    }
    obj
}

fn as_f64(v: &Value) -> Option<f64> {
    v.as_f64()
}

/// A leaf comparison: `resolve(field) <op> value`.
#[derive(Debug, Clone)]
pub struct Comparison {
    pub field: String,
    pub op: String,
    pub value: Value,
}

impl Comparison {
    pub fn new(field: impl Into<String>, op: impl Into<String>, value: Value) -> Self {
        let op = op.into();
        debug_assert!(
            ["==", "!=", ">", "<", ">=", "<=", "~=", "contains", "in"].contains(&op.as_str()),
            "unknown op {op:?}"
        );
        Self {
            field: field.into(),
            op,
            value,
        }
    }

    fn eval(&self, signal: &Value) -> bool {
        let actual = resolve_field(signal, &self.field);
        let expected = &self.value;
        match self.op.as_str() {
            "==" => actual == expected,
            "!=" => actual != expected,
            _ if actual.is_null() => false, // ordering/membership on a missing field never matches
            ">" => cmp(actual, expected, |o| o.is_gt()),
            "<" => cmp(actual, expected, |o| o.is_lt()),
            ">=" => cmp(actual, expected, |o| o.is_ge()),
            "<=" => cmp(actual, expected, |o| o.is_le()),
            "~=" => expected
                .as_str()
                .and_then(|p| Regex::new(p).ok())
                .map(|re| re.is_match(&value_to_string(actual)))
                .unwrap_or(false),
            "contains" => contains(actual, expected),
            "in" => contains(expected, actual),
            _ => false,
        }
    }
}

fn cmp(a: &Value, b: &Value, pick: impl Fn(std::cmp::Ordering) -> bool) -> bool {
    // numeric ordering, else lexicographic on strings (matches Python's > on str/num).
    if let (Some(x), Some(y)) = (as_f64(a), as_f64(b)) {
        return x.partial_cmp(&y).map(pick).unwrap_or(false);
    }
    if let (Some(x), Some(y)) = (a.as_str(), b.as_str()) {
        return pick(x.cmp(y));
    }
    false
}

/// `needle` ∈ `haystack`: substring for strings, membership for arrays.
fn contains(haystack: &Value, needle: &Value) -> bool {
    match haystack {
        Value::String(s) => needle.as_str().map(|n| s.contains(n)).unwrap_or(false),
        Value::Array(arr) => arr.iter().any(|e| e == needle),
        Value::Object(map) => needle
            .as_str()
            .map(|k| map.contains_key(k))
            .unwrap_or(false),
        _ => false,
    }
}

fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// A composable predicate tree evaluated against a signal JSON value.
#[derive(Debug, Clone)]
pub enum Predicate {
    Cmp(Comparison),
    And(Vec<Predicate>),
    Or(Vec<Predicate>),
    Not(Box<Predicate>),
}

impl Predicate {
    pub fn matches(&self, signal: &Value) -> bool {
        match self {
            Predicate::Cmp(c) => c.eval(signal),
            Predicate::And(kids) => kids.iter().all(|k| k.matches(signal)),
            Predicate::Or(kids) => kids.iter().any(|k| k.matches(signal)),
            Predicate::Not(k) => !k.matches(signal),
        }
    }

    /// Reconstruct a declarative predicate tree from its serialized form.
    pub fn from_json(d: &Value) -> Option<Predicate> {
        match d.get("op").and_then(Value::as_str) {
            Some("and") => Some(Predicate::And(Self::kids(d)?)),
            Some("or") => Some(Predicate::Or(Self::kids(d)?)),
            Some("not") => Some(Predicate::Not(Box::new(Self::from_json(d.get("arg")?)?))),
            _ => Some(Predicate::Cmp(Comparison::new(
                d.get("field")?.as_str()?,
                d.get("op")?.as_str()?,
                d.get("value")?.clone(),
            ))),
        }
    }

    fn kids(d: &Value) -> Option<Vec<Predicate>> {
        d.get("args")?
            .as_array()?
            .iter()
            .map(Self::from_json)
            .collect()
    }

    pub fn to_json(&self) -> Value {
        match self {
            Predicate::Cmp(c) => {
                serde_json::json!({"field": c.field, "op": c.op, "value": c.value})
            }
            Predicate::And(k) => {
                serde_json::json!({"op": "and", "args": k.iter().map(Self::to_json).collect::<Vec<_>>()})
            }
            Predicate::Or(k) => {
                serde_json::json!({"op": "or", "args": k.iter().map(Self::to_json).collect::<Vec<_>>()})
            }
            Predicate::Not(k) => serde_json::json!({"op": "not", "arg": k.to_json()}),
        }
    }
}

/// Fluent builder for a declarative comparison: `f("event.status").eq(json!("escalate"))`.
pub fn f(path: &str) -> FieldBuilder {
    FieldBuilder(path.to_string())
}

pub struct FieldBuilder(String);

impl FieldBuilder {
    pub fn eq(self, v: Value) -> Predicate {
        Predicate::Cmp(Comparison::new(self.0, "==", v))
    }
    pub fn ne(self, v: Value) -> Predicate {
        Predicate::Cmp(Comparison::new(self.0, "!=", v))
    }
    pub fn gt(self, v: Value) -> Predicate {
        Predicate::Cmp(Comparison::new(self.0, ">", v))
    }
    pub fn ge(self, v: Value) -> Predicate {
        Predicate::Cmp(Comparison::new(self.0, ">=", v))
    }
    pub fn contains(self, v: Value) -> Predicate {
        Predicate::Cmp(Comparison::new(self.0, "contains", v))
    }
    pub fn matches_regex(self, pattern: &str) -> Predicate {
        Predicate::Cmp(Comparison::new(
            self.0,
            "~=",
            Value::String(pattern.to_string()),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::router::signals::{RoutingEvent, RoutingEventKind, RoutingSignal};
    use serde_json::json;

    fn sig() -> Value {
        let mut ev = RoutingEvent::of(RoutingEventKind::ToolResult);
        ev.status = Some("escalate".into());
        ev.duration_ms = Some(1200);
        ev.text = Some("please transfer me to billing".into());
        RoutingSignal::new(ev).to_value()
    }

    #[test]
    fn resolves_dotted_path() {
        let s = sig();
        assert_eq!(resolve_field(&s, "event.status"), &json!("escalate"));
        assert_eq!(resolve_field(&s, "event.kind"), &json!("tool_result"));
        assert_eq!(resolve_field(&s, "event.missing"), &Value::Null);
        assert_eq!(resolve_field(&s, "active_agent.id"), &Value::Null);
    }

    #[test]
    fn equality_and_enum_value_match() {
        assert!(f("event.status").eq(json!("escalate")).matches(&sig()));
        assert!(f("event.kind").eq(json!("tool_result")).matches(&sig()));
        assert!(!f("event.status").eq(json!("nope")).matches(&sig()));
    }

    #[test]
    fn ordering_on_missing_is_false() {
        assert!(!f("event.retriable").gt(json!(0)).matches(&sig())); // null → false
        assert!(f("event.duration_ms").gt(json!(1000)).matches(&sig()));
        assert!(!f("event.duration_ms").gt(json!(2000)).matches(&sig()));
    }

    #[test]
    fn regex_contains_and_bool_ops() {
        assert!(f("event.text")
            .matches_regex("transfer.*billing")
            .matches(&sig()));
        assert!(f("event.text").contains(json!("billing")).matches(&sig()));
        let both = Predicate::And(vec![
            f("event.status").eq(json!("escalate")),
            f("event.duration_ms").ge(json!(1200)),
        ]);
        assert!(both.matches(&sig()));
        let neg = Predicate::Not(Box::new(f("event.status").eq(json!("escalate"))));
        assert!(!neg.matches(&sig()));
    }

    #[test]
    fn json_roundtrip() {
        let p = Predicate::Or(vec![
            f("event.status").eq(json!("escalate")),
            f("event.duration_ms").gt(json!(5000)),
        ]);
        let round = Predicate::from_json(&p.to_json()).unwrap();
        assert_eq!(round.matches(&sig()), p.matches(&sig()));
        assert!(round.matches(&sig()));
    }
}
