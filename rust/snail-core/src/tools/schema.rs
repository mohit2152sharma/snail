//! Common-denominator schema validator (port of `snail.tools.schema`).
//!
//! The neutral schema dialect is the common denominator of both vendors — portable, constrained,
//! no per-vendor branching. Supported keywords: `type`
//! (object/array/string/number/integer/boolean/null), `properties`, `required`, `items`, `enum`,
//! `nullable`. Returns `None` when valid, else a short human-readable error string (used to build
//! the model-facing `invalid_args` reason so it can self-correct).

use serde_json::Value;

fn type_ok(t: &str, v: &Value) -> Option<bool> {
    Some(match t {
        "string" => v.is_string(),
        // bool is excluded from number/integer (matches Python's isinstance(bool) guard).
        "number" => v.is_number(),
        "integer" => v.is_i64() || v.is_u64(),
        "boolean" => v.is_boolean(),
        "object" => v.is_object(),
        "array" => v.is_array(),
        "null" => v.is_null(),
        _ => return None, // unknown type
    })
}

/// Validate `value` against `schema`. Return `None` if valid, else an error string.
pub fn validate(value: &Value, schema: Option<&Value>, path: &str) -> Option<String> {
    let schema = schema?;
    let where_ = if path.is_empty() { "value" } else { path };

    if value.is_null() {
        let nullable = schema
            .get("nullable")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let is_null_type = schema.get("type").and_then(Value::as_str) == Some("null");
        if nullable || is_null_type {
            return None;
        }
        return Some(format!("{where_}: null not allowed"));
    }

    let t = schema.get("type").and_then(Value::as_str);
    if let Some(t) = t {
        match type_ok(t, value) {
            None => return Some(format!("{where_}: unknown schema type '{t}'")),
            Some(false) => return Some(format!("{where_}: expected {t}")),
            Some(true) => {}
        }
    }

    if let Some(Value::Array(allowed)) = schema.get("enum") {
        if !allowed.iter().any(|a| a == value) {
            return Some(format!("{where_}: {value} not in enum"));
        }
    }

    if t == Some("object") {
        if let Some(obj) = value.as_object() {
            if let Some(Value::Array(required)) = schema.get("required") {
                for req in required {
                    if let Some(key) = req.as_str() {
                        if !obj.contains_key(key) {
                            return Some(format!("{where_}.{key}: required field missing"));
                        }
                    }
                }
            }
            if let Some(Value::Object(props)) = schema.get("properties") {
                for (key, sub) in props {
                    if let Some(v) = obj.get(key) {
                        let child = if path.is_empty() {
                            key.clone()
                        } else {
                            format!("{path}.{key}")
                        };
                        let err = validate(v, Some(sub), &child);
                        if err.is_some() {
                            return err;
                        }
                    }
                }
            }
        }
    }

    if t == Some("array") {
        if let (Some(items_schema), Some(arr)) = (schema.get("items"), value.as_array()) {
            for (i, el) in arr.iter().enumerate() {
                let err = validate(el, Some(items_schema), &format!("{where_}[{i}]"));
                if err.is_some() {
                    return err;
                }
            }
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn none_schema_is_always_valid() {
        assert!(validate(&json!(123), None, "").is_none());
    }

    #[test]
    fn type_mismatch_reports_path() {
        let s = json!({"type": "object", "properties": {"n": {"type": "integer"}}});
        assert_eq!(
            validate(&json!({"n": "x"}), Some(&s), ""),
            Some("n: expected integer".to_string())
        );
    }

    #[test]
    fn bool_is_not_integer_or_number() {
        assert_eq!(
            validate(&json!(true), Some(&json!({"type": "integer"})), ""),
            Some("value: expected integer".to_string())
        );
        assert_eq!(
            validate(&json!(true), Some(&json!({"type": "number"})), ""),
            Some("value: expected number".to_string())
        );
    }

    #[test]
    fn required_missing() {
        let s = json!({"type": "object", "required": ["a"]});
        assert_eq!(
            validate(&json!({"b": 1}), Some(&s), ""),
            Some("value.a: required field missing".to_string())
        );
    }

    #[test]
    fn nullable_allows_null() {
        assert!(validate(
            &json!(null),
            Some(&json!({"type": "string", "nullable": true})),
            ""
        )
        .is_none());
        assert_eq!(
            validate(&json!(null), Some(&json!({"type": "string"})), ""),
            Some("value: null not allowed".to_string())
        );
    }

    #[test]
    fn enum_membership() {
        let s = json!({"enum": ["a", "b"]});
        assert!(validate(&json!("a"), Some(&s), "").is_none());
        assert!(validate(&json!("z"), Some(&s), "").is_some());
    }

    #[test]
    fn array_items_validated() {
        let s = json!({"type": "array", "items": {"type": "integer"}});
        assert!(validate(&json!([1, 2, 3]), Some(&s), "").is_none());
        assert_eq!(
            validate(&json!([1, "x"]), Some(&s), ""),
            Some("value[1]: expected integer".to_string())
        );
    }
}
