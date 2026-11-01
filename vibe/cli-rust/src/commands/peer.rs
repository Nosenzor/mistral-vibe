//! The `/peer` slash command, the panel toggle's config read, and the
//! panel's steer action.

use std::sync::Arc;

use super::event::{dispatch, CommandEvent};
use crate::app::App;
use crate::peer::{PeerConfig, ReviewStatus};
use crate::server::{method, Client};

const USAGE: &str = "Usage: /peer [model|off] [steer on|off]";

/// What `/peer` should do with its argument.
#[derive(Debug, PartialEq)]
pub enum PeerArgs {
    Read,
    Configure {
        model: Option<String>,
        disable: bool,
        auto_steer: Option<bool>,
    },
}

/// Parse the `/peer` argument: empty reads, `off` disables, a model alias
/// enables, and `steer on|off` toggles auto-steer (alone or after a model).
pub fn parse(arg: &str) -> Result<PeerArgs, String> {
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
        Err(usage) => {
            let _ = tx.try_send(CommandEvent::PeerConfig {
                result: Err(usage),
                announce: true,
            });
            return;
        }
    };
    let client = client.clone();
    tokio::spawn(async move {
        let result = client.request(request, params).await;
        let _ = tx.try_send(config_event(result, true));
    });
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

/// Enter in the focused panel: steer the selected critique into the turn.
pub fn steer_selected(app: &mut App, client: &Arc<Client>) {
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
