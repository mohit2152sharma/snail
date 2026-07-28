//! Client-facing transport (port of `snail.transport`). The control-frame [`protocol`] is here;
//! the async websocket server + client bridge land alongside the connection layer.

pub mod protocol;

pub use protocol::{decode_control, encode_control, Control, ControlType};
