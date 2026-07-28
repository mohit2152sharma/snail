//! Tool-call registry (port of `snail.registry`): the in-flight [`call_registry::ToolCallRegistry`]
//! that guards single-resolution, plus the [`pending`] FSM + loop-agnostic promise.

pub mod call_registry;
pub mod pending;

pub use call_registry::{RegisterError, RegisterOpts, ToolCallRegistry};
pub use pending::{CallState, Destination, PendingCall, Promise};
