//! The `/peer` slash command, the panel toggle's config read, and the
//! panel's steer action.

use std::sync::Arc;

use super::event::{dispatch, CommandEvent};
use crate::app::App;
use crate::peer::{PeerConfig, ReviewStatus};
use crate::server::{method, Client};

/// `/peer`: no argument reads the state, `<model>` enables the duck for this
/// session, `off` disables it.
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
    let (request, params) = match arg {
        "" => (
            method::PEER_READ,
            serde_json::json!({ "sessionId": session_id }),
        ),
        "off" => (
            method::PEER_CONFIGURE,
            serde_json::json!({ "sessionId": session_id, "model": null }),
        ),
        model => (
            method::PEER_CONFIGURE,
            serde_json::json!({ "sessionId": session_id, "model": model }),
        ),
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
