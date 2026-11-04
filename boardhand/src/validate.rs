//! Validates arguments against the JSON Schema subset the tool schemas use.
//!
//! A unit test in `tools.rs` walks every advertised schema and fails on any
//! keyword not handled here, so a schema can't promise a check this skips.
use serde_json::Value;

pub const SHA256_PATTERN: &str = "^[0-9a-f]{64}$";
pub const KEYWORDS: &[&str] = &[
    "type", "required", "properties", "additionalProperties", "items", "minimum", "maximum",
    "minLength", "maxLength", "minItems", "maxItems", "enum", "anyOf", "pattern", "default",
    "description",
];

#[derive(Debug, PartialEq, Eq)]
pub struct Invalid {
    pub path: String,
    pub message: String,
}

fn fail(path: &str, message: String) -> Invalid {
    let path = if path.is_empty() { "arguments".to_string() } else { path.to_string() };
    Invalid { path, message }
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

fn is_integer(v: &Value) -> bool {
    match v {
        Value::Number(n) => n.is_i64() || n.is_u64() || n.as_f64().is_some_and(|f| f.fract() == 0.0 && f.abs() < 9.0e15),
        _ => false,
    }
}

fn matches_type(v: &Value, ty: &str) -> bool {
    match ty {
        "integer" => is_integer(v),
        other => type_name(v) == other,
    }
}

pub fn validate(schema: &Value, v: &Value, path: &str) -> Result<(), Invalid> {
    if let Some(ty) = schema.get("type").and_then(Value::as_str) {
        if !matches_type(v, ty) {
            let what = if ty == "integer" && v.is_number() { format!("fractional number {v}") } else { type_name(v).to_string() };
            return Err(fail(path, format!("expected {ty}, got {what}")));
        }
    }
    if let Some(any) = schema.get("anyOf").and_then(Value::as_array) {
        let mut same_type_error = None;
        let mut ok = false;
        for branch in any {
            match validate(branch, v, path) {
                Ok(()) => ok = true,
                Err(e) => {
                    let ty = branch.get("type").and_then(Value::as_str).unwrap_or("");
                    if matches_type(v, ty) && same_type_error.is_none() {
                        same_type_error = Some(e);
                    }
                }
            }
        }
        if !ok {
            return Err(same_type_error.unwrap_or_else(|| fail(path, format!("{} is not an accepted form", type_name(v)))));
        }
    }
    if let Some(allowed) = schema.get("enum").and_then(Value::as_array) {
        if !allowed.contains(v) {
            return Err(fail(path, format!("must be one of {}", Value::Array(allowed.clone()))));
        }
    }
    if let Some(n) = v.as_f64() {
        if let Some(min) = schema.get("minimum").and_then(Value::as_f64) {
            if n < min {
                return Err(fail(path, format!("{n} is below the minimum {min}")));
            }
        }
        if let Some(max) = schema.get("maximum").and_then(Value::as_f64) {
            if n > max {
                return Err(fail(path, format!("{n} is above the maximum {max}")));
            }
        }
    }
    if let Some(s) = v.as_str() {
        let len = s.chars().count() as u64;
        if schema.get("minLength").and_then(Value::as_u64).is_some_and(|m| len < m) {
            return Err(fail(path, "string is too short".into()));
        }
        if schema.get("maxLength").and_then(Value::as_u64).is_some_and(|m| len > m) {
            return Err(fail(path, "string is too long".into()));
        }
        if let Some(p) = schema.get("pattern").and_then(Value::as_str) {
            assert_eq!(p, SHA256_PATTERN, "unsupported pattern in a tool schema");
            if s.len() != 64 || !s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
                return Err(fail(path, "must be a lowercase SHA-256 hex string".into()));
            }
        }
    }
    if let Some(a) = v.as_array() {
        if schema.get("minItems").and_then(Value::as_u64).is_some_and(|m| (a.len() as u64) < m) {
            return Err(fail(path, "array has too few items".into()));
        }
        if schema.get("maxItems").and_then(Value::as_u64).is_some_and(|m| (a.len() as u64) > m) {
            return Err(fail(path, "array has too many items".into()));
        }
        if let Some(items) = schema.get("items") {
            for (i, e) in a.iter().enumerate() {
                validate(items, e, &format!("{path}[{i}]"))?;
            }
        }
    }
    if let Some(o) = v.as_object() {
        let join = |k: &str| if path.is_empty() { k.to_string() } else { format!("{path}.{k}") };
        if let Some(req) = schema.get("required").and_then(Value::as_array) {
            for r in req.iter().filter_map(Value::as_str) {
                if !o.contains_key(r) {
                    return Err(fail(&join(r), "required field is missing".into()));
                }
            }
        }
        let props = schema.get("properties").and_then(Value::as_object);
        let closed = schema.get("additionalProperties") == Some(&Value::Bool(false));
        for (k, val) in o {
            match props.and_then(|p| p.get(k)) {
                Some(sub) => validate(sub, val, &join(k))?,
                None if closed => return Err(fail(&join(k), "unknown field".into())),
                None => {}
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn schema() -> Value {
        json!({"type":"object","required":["items"],"additionalProperties":false,"properties":{
            "n":{"type":"integer","minimum":1,"maximum":5},
            "g":{"anyOf":[{"type":"string"},{"type":"array","items":{"type":"string"},"minItems":1}]},
            "h":{"type":"string","pattern":SHA256_PATTERN},
            "items":{"type":"array","items":{"type":"object","required":["width"],"additionalProperties":false,
                "properties":{"width":{"type":"number","minimum":0.1}}}}
        }})
    }

    fn path(v: Value) -> String {
        validate(&schema(), &v, "").unwrap_err().path
    }

    #[test]
    fn field_paths() {
        assert_eq!(path(json!({"items":[{"width":1},{"width":1},{"width":"x"}]})), "items[2].width");
        assert_eq!(path(json!({"items":[{}]})), "items[0].width");
        assert_eq!(path(json!({"items":[{"width":1,"z":1}]})), "items[0].z");
        assert_eq!(path(json!({})), "items");
        assert_eq!(path(json!({"items":[],"x":1})), "x");
        assert_eq!(path(json!([])), "arguments");
        assert_eq!(path(json!({"items":[{"width":0.01}]})), "items[0].width");
    }

    #[test]
    fn integers() {
        assert_eq!(path(json!({"items":[],"n":2.5})), "n");
        assert_eq!(path(json!({"items":[],"n":"2"})), "n");
        assert_eq!(path(json!({"items":[],"n":true})), "n");
        assert_eq!(path(json!({"items":[],"n":6})), "n");
        assert!(validate(&schema(), &json!({"items":[],"n":2}), "").is_ok());
        // JSON Schema treats 2.0 as an integer. It isn't fractional.
        assert!(validate(&schema(), &json!({"items":[],"n":2.0}), "").is_ok());
    }

    #[test]
    fn any_of_reports_nested_path() {
        assert_eq!(path(json!({"items":[],"g":["a",1]})), "g[1]");
        assert_eq!(path(json!({"items":[],"g":3})), "g");
        assert!(validate(&schema(), &json!({"items":[],"g":"a"}), "").is_ok());
        assert_eq!(path(json!({"items":[],"g":[]})), "g");
    }

    #[test]
    fn hash_pattern() {
        assert_eq!(path(json!({"items":[],"h":"ABC"})), "h");
        assert_eq!(path(json!({"items":[],"h":"A".repeat(64)})), "h");
        assert!(validate(&schema(), &json!({"items":[],"h":"a".repeat(64)}), "").is_ok());
    }
}
