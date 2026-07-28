//! The Tool object — stateless, vendor-independent, reusable (port of `snail.tools.tool`).
//!
//! No result state lives on a Tool: it is reused across agents and concurrent calls. The handler
//! is the pure, sync envelope path (validated args → an `output_schema`-shaped value, or an error
//! string). Async handlers + timeouts are the session/runtime layer's job (docs 06).

use serde_json::Value;

use crate::vendor::params::ToolSpec;

/// A handler maps validated args → a neutral value, or `Err(message)` (the raw message is
/// log-only; the model gets a sanitized reason). `Send + Sync` so a tool is shareable across the
/// runtime.
pub type ToolHandler = Box<dyn Fn(&Value) -> Result<Value, String> + Send + Sync>;

/// `name + input_schema + output_schema + handler` — stateless.
pub struct Tool {
    pub name: String,
    pub handler: ToolHandler,
    pub description: String,
    pub input_schema: Option<Value>,
    pub output_schema: Value,
    pub is_framework: bool,
    pub non_blocking: bool,
    pub timeout_s: Option<f64>,
}

impl Tool {
    pub fn new(name: impl Into<String>, output_schema: Value, handler: ToolHandler) -> Self {
        let name = name.into();
        assert!(!name.is_empty(), "Tool.name is required");
        Self {
            name,
            handler,
            description: String::new(),
            input_schema: None,
            output_schema,
            is_framework: false,
            non_blocking: false,
            timeout_s: None,
        }
    }

    pub fn with_input_schema(mut self, schema: Value) -> Self {
        self.input_schema = Some(schema);
        self
    }
    pub fn with_description(mut self, description: impl Into<String>) -> Self {
        self.description = description.into();
        self
    }
    pub fn non_blocking(mut self, yes: bool) -> Self {
        self.non_blocking = yes;
        self
    }

    /// The vendor-neutral declaration bound at setup (exposure).
    pub fn to_spec(&self) -> ToolSpec {
        ToolSpec {
            name: self.name.clone(),
            description: self.description.clone(),
            parameters: self.input_schema.clone(),
            non_blocking: self.non_blocking,
        }
    }
}
