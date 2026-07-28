//! Client ⇄ Snail wire protocol (port of `snail.transport.protocol`).
//!
//! One WebSocket carries two frame kinds: **binary → media** (raw PCM16LE mono, no header — the
//! bytes are the payload) and **text → control** (a JSON [`Control`]). The control channel is what
//! makes a real barge-in possible: revoking the server token stops *sending*, but only a
//! client-bound `flush` cuts the client's already-buffered playout.

use serde::{Deserialize, Serialize};

/// Control message kinds on the text channel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlType {
    // server → client
    Ready,
    Flush,
    Transcript,
    Bye,
    // client → server
    Playout,
    End,
}

/// One control-channel message. Unused fields are omitted on the wire (matches msgspec
/// `omit_defaults`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Control {
    #[serde(rename = "type")]
    pub kind: ControlType,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub samples: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

impl Control {
    /// A bare control message of `kind` with all optional fields unset.
    pub fn of(kind: ControlType) -> Self {
        Self {
            kind,
            text: None,
            role: None,
            final_: None,
            samples: None,
            reason: None,
        }
    }
}

/// Serialize a control message to a WS text frame.
pub fn encode_control(control: &Control) -> String {
    serde_json::to_string(control).expect("Control serializes")
}

/// Parse a WS text frame into a control message.
pub fn decode_control(text: &str) -> Result<Control, serde_json::Error> {
    serde_json::from_str(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_encodes_without_none_fields() {
        let s = encode_control(&Control::of(ControlType::Ready));
        assert_eq!(s, r#"{"type":"ready"}"#);
    }

    #[test]
    fn playout_roundtrips_with_samples() {
        let c = Control {
            samples: Some(4800),
            ..Control::of(ControlType::Playout)
        };
        let s = encode_control(&c);
        assert_eq!(s, r#"{"type":"playout","samples":4800}"#);
        assert_eq!(decode_control(&s).unwrap(), c);
    }

    #[test]
    fn decode_end_control() {
        assert_eq!(
            decode_control(r#"{"type":"end"}"#).unwrap().kind,
            ControlType::End
        );
    }
}
