//! Vendor-neutral tool layer (port of `snail.tools`): the [`result::ToolResult`] envelope and the
//! common-denominator [`schema::validate`] validator. The `Tool`/registry dispatch that binds a
//! handler is loop-bound and lands with the session migration.

pub mod result;
pub mod schema;

pub use result::{DirectiveMode, ResponseMode, SpeakDirective, ToolResult, ToolStatus};
pub use schema::validate;
