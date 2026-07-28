//! Standalone all-Rust snail-rt server + a minimal browser test page.
//!
//! Run (needs a Dev-API key):
//!     GEMINI_API_KEY=... cargo run -p snail-rt --example serve --features server
//! then open http://localhost:8000 in Chrome, click Start, and talk.
//!
//! The full path is Rust end to end: browser WS → ClientBridge → run_gemini_agent →
//! split Gemini WebSocket. No Python. Override the model with SNAIL_MODEL (default is a
//! Dev-API live model); the passthrough leans on Gemini's built-in VAD for turn-taking.

use axum::response::Html;
use axum::routing::get;

use snail_core::vendor::{Backend, SetupParam};
use snail_rt::connection::AgentSpec;
use snail_rt::serve::{app, ServeConfig};

#[tokio::main]
async fn main() {
    let api_key = std::env::var("GEMINI_API_KEY").expect("set GEMINI_API_KEY (Dev-API key)");
    let model = std::env::var("SNAIL_MODEL").unwrap_or_else(|_| "gemini-2.0-flash-live-001".into());

    let mut setup = SetupParam::new(model.clone());
    setup.system_instruction =
        "You are a friendly voice assistant. Keep spoken replies short and natural.".into();
    let spec = AgentSpec {
        id: "agent".into(),
        backend: Backend::GeminiDev,
        setup,
    };
    let config = ServeConfig {
        api_key,
        spec,
        input_sample_rate: 16000,
    };

    let router = app(config).route("/", get(|| async { Html(include_str!("test-page.html")) }));

    let addr = "0.0.0.0:8000";
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind :8000");
    println!("snail-rt server on http://localhost:8000  (model={model})");
    println!("open Chrome at http://localhost:8000, click Start, talk.");
    axum::serve(listener, router).await.expect("serve");
}
