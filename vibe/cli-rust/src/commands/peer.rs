//! The `/peer` slash command, the panel toggle's config read, and the
//! panel's steer action.

use std::sync::Arc;

use super::event::{dispatch, CommandEvent};
use crate::app::App;
use crate::peer::{PeerConfig, ReviewStatus};
use crate::server::{method, Client};

const USAGE: &str =
    "Usage: /peer [model|off] [steer on|off] | run <task> | agent <task> | stop [runId]";

/// What `/peer` should do with its argument.
#[derive(Debug, PartialEq)]
pub enum PeerArgs {
    Read,
    Configure {
        model: Option<String>,
        disable: bool,
        auto_steer: Option<bool>,
    },
    /// `/peer run <task>`: one-off task on the duck, answered in the panel.
    Run {
        prompt: String,
    },
    /// `/peer agent <task>`: the duck as an independent background agent.
    Agent {
        prompt: String,
    },
    /// `/peer stop [runId]`: cancel a running run or agent.
    Stop {
        run_id: Option<String>,
    },
}

/// Parse the `/peer` argument: empty reads, `off` disables, a model alias
/// enables, and `steer on|off` toggles auto-steer (alone or after a model).
pub fn parse(arg: &str) -> Result<PeerArgs, String> {
    // `run`, `agent` and `stop` are reserved subcommands.
    if let Some((head, rest)) = arg.split_once(char::is_whitespace) {
        let rest = rest.trim();
        match head {
            "run" if !rest.is_empty() => {
                return Ok(PeerArgs::Run {
                    prompt: rest.to_owned(),
                })
            }
            "agent" if !rest.is_empty() => {
                return Ok(PeerArgs::Agent {
                    prompt: rest.to_owned(),
                })
            }
            "stop" if rest.is_empty() => return Ok(PeerArgs::Stop { run_id: None }),
            "stop" => {
                return Ok(PeerArgs::Stop {
                    run_id: Some(rest.to_owned()),
                })
            }
            _ => {}
        }
    }
    if arg == "stop" {
        return Ok(PeerArgs::Stop { run_id: None });
    }
    if arg == "run" || arg == "agent" {
        return Err(USAGE.to_owned());
    }
    // `off` is a reserved word, valid only as the whole argument.
    if arg == "off" {
        return Ok(PeerArgs::Configure {
            model: None,
            disable: true,
            auto_steer: None,
        });
    }
    let mut model: Option<String> = None;
    let mut auto_steer: Option<bool> = None;
    let mut tokens = arg.split_whitespace();
    while let Some(token) = tokens.next() {
        match token {
            "steer" => match tokens.next() {
                Some("on") if auto_steer.is_none() => auto_steer = Some(true),
                Some("off") if auto_steer.is_none() => auto_steer = Some(false),
                _ => return Err(USAGE.to_owned()),
            },
            "off" => return Err(USAGE.to_owned()),
            other => {
                if model.is_some() {
                    return Err(USAGE.to_owned());
                }
                model = Some(other.to_owned());
            }
        }
    }
    if model.is_none() && auto_steer.is_none() {
        return Ok(PeerArgs::Read);
    }
    Ok(PeerArgs::Configure {
        model,
        disable: false,
        auto_steer,
    })
}

/// `/peer`: no argument reads the state, `<model>` enables the duck for this
/// session, `off` disables it, `steer on|off` toggles auto-steer.
pub fn run(app: &mut App, client: &Arc<Client>, value: &str) {
    let arg = value
        .split_once(char::is_whitespace)
        .map(|(_, rest)| rest)
        .unwrap_or_default()
        .trim();
    let Some((session_id, tx)) = dispatch(app) else {
        return;
    };
    // Build the request before the spawn: `arg` borrows `value`, which the
    // spawned task must not capture.
    let (request, params) = match parse(arg) {
        Ok(PeerArgs::Read) => (
            method::PEER_READ,
            serde_json::json!({ "sessionId": session_id }),
        ),
        Ok(PeerArgs::Configure {
            model,
            disable,
            auto_steer,
        }) => {
            let mut params = serde_json::json!({ "sessionId": session_id });
            if let Some(model) = model {
                params["model"] = serde_json::json!(model);
            }
            if disable {
                params["disable"] = serde_json::json!(true);
            }
            if let Some(auto_steer) = auto_steer {
                params["autoSteer"] = serde_json::json!(auto_steer);
            }
            (method::PEER_CONFIGURE, params)
        }
        Ok(PeerArgs::Run { prompt }) => (
            method::PEER_RUN,
            serde_json::json!({ "sessionId": session_id, "prompt": prompt }),
        ),
        Ok(PeerArgs::Agent { prompt }) => (
            method::PEER_AGENT,
            serde_json::json!({ "sessionId": session_id, "prompt": prompt }),
        ),
        Ok(PeerArgs::Stop { run_id }) => (
            method::PEER_STOP,
            match run_id {
                Some(run_id) => serde_json::json!({ "sessionId": session_id, "runId": run_id }),
                None => serde_json::json!({ "sessionId": session_id }),
            },
        ),
        Err(usage) => {
            let _ = tx.try_send(CommandEvent::PeerConfig {
                result: Err(usage),
                announce: true,
            });
            return;
        }
    };
    let is_config = matches!(request, method::PEER_READ | method::PEER_CONFIGURE);
    let client = client.clone();
    crate::work::spawn(async move {
        let result = client.request(request, params).await;
        let event = if is_config {
            config_event(result, true)
        } else {
            run_event(request, result)
        };
        let _ = tx.try_send(event);
    });
}

/// The `/peer run|agent|stop` answer: a status line; the run itself reports
/// through the `peer/result` notification into the panel.
fn run_event(request: &str, result: Result<serde_json::Value, anyhow::Error>) -> CommandEvent {
    let label = match request {
        method::PEER_RUN => "run",
        method::PEER_AGENT => "agent",
        _ => "stop",
    };
    let text = match result {
        Ok(value) => match value.get("runId").and_then(serde_json::Value::as_str) {
            Some(run_id) => {
                format!("Peer {label} started as {run_id}; the result lands in the Ctrl+P panel.")
            }
            None => format!("Peer {label} accepted."),
        },
        Err(error) => return CommandEvent::Status(format!("Peer {label} failed: {error}")),
    };
    CommandEvent::Status(text)
}

/// The Ctrl+P toggle: open (or close) the panel, and on open read the config
/// silently so the header shows the duck model without a `/peer` round-trip.
pub fn toggle_panel(app: &mut App, client: &Arc<Client>) {
    let opening = !app.peer.open;
    app.peer.toggle();
    if opening {
        read_config(app, client, false);
    }
}

/// Read the peer config; `announce` mounts the state text and opens the panel.
fn read_config(app: &mut App, client: &Arc<Client>, announce: bool) {
    let Some((session_id, tx)) = dispatch(app) else {
        return;
    };
    let client = client.clone();
    tokio::spawn(async move {
        let result = client
            .request(
                method::PEER_READ,
                serde_json::json!({ "sessionId": session_id }),
            )
            .await;
        let _ = tx.try_send(config_event(result, announce));
    });
}

fn config_event(result: Result<serde_json::Value, anyhow::Error>, announce: bool) -> CommandEvent {
    let result = match result {
        Ok(value) => match PeerConfig::from_value(&value) {
            Some(config) => Ok(config),
            None => Err("invalid peer config response".to_owned()),
        },
        Err(error) => Err(error.to_string()),
    };
    CommandEvent::PeerConfig { result, announce }
}

/// Enter in the focused panel: steer the selected critique or agent report
/// into the turn.
pub fn steer_selected(app: &mut App, client: &Arc<Client>) {
    // A selected run steers its report; only finished, steerable, undelivered.
    if let Some(run) = app.peer.selected_run() {
        if run.status != crate::peer::RunStatus::Complete || !run.steerable || run.delivered {
            return;
        }
        let run_id = run.run_id.clone();
        let Some((session_id, tx)) = dispatch(app) else {
            return;
        };
        let client = client.clone();
        crate::work::spawn(async move {
            let result = client
                .request(
                    method::PEER_STEER,
                    serde_json::json!({ "sessionId": session_id, "reviewId": run_id }),
                )
                .await;
            let result = match result {
                Ok(value) => Ok(value
                    .get("delivered")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or_default()),
                Err(error) => Err(error.to_string()),
            };
            let _ = tx.try_send(CommandEvent::PeerSteer {
                review_id: run_id,
                result,
            });
        });
        return;
    }
    let Some(review) = app.peer.selected_review() else {
        return;
    };
    // Only a finished critique can be steered, and only once.
    if review.status != ReviewStatus::Complete || review.delivered {
        return;
    }
    let review_id = review.review_id.clone();
    let Some((session_id, tx)) = dispatch(app) else {
        return;
    };
    let client = client.clone();
    tokio::spawn(async move {
        let result = client
            .request(
                method::PEER_STEER,
                serde_json::json!({ "sessionId": session_id, "reviewId": review_id }),
            )
            .await;
        let result = match result {
            Ok(value) => Ok(value
                .get("delivered")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or_default()),
            Err(error) => Err(error.to_string()),
        };
        let _ = tx.try_send(CommandEvent::PeerSteer { review_id, result });
    });
}

/// `d` on a running run in the focused panel: cancel it via `peer/stop`.
pub fn stop_selected(app: &mut App, client: &Arc<Client>) {
    let Some(run) = app.peer.selected_run() else {
        return;
    };
    if run.status != crate::peer::RunStatus::Running {
        return;
    }
    let run_id = run.run_id.clone();
    let Some((session_id, tx)) = dispatch(app) else {
        return;
    };
    let client = client.clone();
    crate::work::spawn(async move {
        let result = client
            .request(
                method::PEER_STOP,
                serde_json::json!({ "sessionId": session_id, "runId": run_id }),
            )
            .await;
        let text = match result {
            Ok(_) => format!("Peer run {run_id} cancelled."),
            Err(error) => format!("Failed to cancel peer run {run_id}: {error}"),
        };
        let _ = tx.try_send(CommandEvent::Status(text));
    });
}

/// The `/peer` result text: the state the panel header also renders.
pub fn config_text(config: &PeerConfig) -> String {
    if !config.enabled {
        return "Peer review is off. Enable it with `/peer <model>` for this session.".to_owned();
    }
    let model = config.model.as_deref().unwrap_or("unknown");
    let steer = if config.auto_steer {
        format!(
            "auto-steer at {}",
            crate::peer::severity_label(config.auto_steer_severity)
        )
    } else {
        "auto-steer off".to_owned()
    };
    format!(
        "Peer review is on: duck `{model}`, {steer}, up to {} reviews per turn.",
        config.max_reviews_per_turn
    )
}
