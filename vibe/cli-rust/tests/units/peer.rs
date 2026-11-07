//! The peer panel's state: wire parsing, upsert, cursor, dismiss, and the
//! first-critique auto-dock.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::json;
use vibe_rs::app::App;
use vibe_rs::peer::{self, PeerConfig, PeerReview, PeerState, ReviewStatus, Severity, MAX_REVIEWS};

fn review(id: &str) -> PeerReview {
    PeerReview {
        review_id: id.to_owned(),
        turn_id: "turn-1".to_owned(),
        status: ReviewStatus::Complete,
        severity: Severity::Warning,
        steerable: true,
        delivered: false,
        text: format!("critique {id}"),
        error: None,
    }
}

fn running(id: &str) -> PeerReview {
    PeerReview {
        status: ReviewStatus::Running,
        severity: Severity::Info,
        ..review(id)
    }
}

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

#[test]
fn review_params_parse_every_field() {
    let parsed = PeerReview::from_value(&json!({
        "reviewId": "r1",
        "turnId": "t1",
        "status": "complete",
        "severity": "critical",
        "steerable": true,
        "delivered": true,
        "text": "off plan",
        "error": null,
    }))
    .expect("a full payload parses");
    assert_eq!(parsed.review_id, "r1");
    assert_eq!(parsed.turn_id, "t1");
    assert_eq!(parsed.status, ReviewStatus::Complete);
    assert_eq!(parsed.severity, Severity::Critical);
    assert!(parsed.steerable);
    assert!(parsed.delivered);
    assert_eq!(parsed.text, "off plan");
    assert_eq!(parsed.error, None);
}

#[test]
fn review_params_default_the_optional_fields() {
    let parsed = PeerReview::from_value(&json!({
        "reviewId": "r1",
        "status": "running",
        "severity": null,
        "error": null,
    }))
    .expect("a minimal payload parses");
    assert_eq!(parsed.status, ReviewStatus::Running);
    assert_eq!(parsed.severity, Severity::Info);
    assert!(!parsed.steerable);
    assert!(!parsed.delivered);
    assert!(parsed.text.is_empty());
    assert!(PeerReview::from_value(&json!({"status": "running"})).is_none());
}

#[test]
fn config_view_parses_and_requires_enabled() {
    let parsed = PeerConfig::from_value(&json!({
        "enabled": true,
        "model": "glm-5.3",
        "autoSteer": true,
        "maxReviewsPerTurn": 10,
        "autoSteerSeverity": "critical",
    }))
    .expect("a full view parses");
    assert!(parsed.enabled);
    assert_eq!(parsed.model.as_deref(), Some("glm-5.3"));
    assert!(parsed.auto_steer);
    assert_eq!(parsed.max_reviews_per_turn, 10);
    assert_eq!(parsed.auto_steer_severity, Severity::Critical);
    assert!(PeerConfig::from_value(&json!({"model": "glm-5.3"})).is_none());
}

#[test]
fn upsert_appends_then_updates_in_place() {
    let mut state = PeerState::default();
    state.upsert(running("r1"));
    state.upsert(review("r2"));
    assert_eq!(state.reviews.len(), 2);
    assert_eq!(state.selected, 1, "the cursor follows the tail");
    let mut complete = review("r1");
    complete.status = ReviewStatus::Complete;
    complete.severity = Severity::Critical;
    state.upsert(complete);
    assert_eq!(state.reviews.len(), 2, "an update never duplicates");
    assert_eq!(state.reviews[0].severity, Severity::Critical);
    assert_eq!(state.selected, 1, "an update never moves the cursor");
}

#[test]
fn upsert_drops_the_oldest_past_the_bound() {
    let mut state = PeerState::default();
    for index in 0..MAX_REVIEWS + 5 {
        state.upsert(review(&format!("r{index}")));
    }
    assert_eq!(state.reviews.len(), MAX_REVIEWS);
    assert_eq!(state.reviews[0].review_id, "r5");
    assert_eq!(state.selected, MAX_REVIEWS - 1);
}

#[test]
fn an_evicted_oldest_review_keeps_an_off_tail_cursor_on_the_same_review() {
    let mut state = PeerState::default();
    for index in 0..MAX_REVIEWS {
        state.upsert(review(&format!("r{index}")));
    }
    state.move_selection(false);
    let parked = state.reviews[state.selected].review_id.clone();
    state.upsert(review("overflow"));
    assert_eq!(state.reviews.len(), MAX_REVIEWS);
    assert_eq!(
        state.reviews[state.selected].review_id, parked,
        "the cursor stays on the same review across an eviction"
    );
}

#[test]
fn a_cursor_off_the_tail_stays_put() {
    let mut state = PeerState::default();
    state.upsert(review("r1"));
    state.upsert(review("r2"));
    state.move_selection(false);
    assert_eq!(state.selected, 0);
    state.upsert(review("r3"));
    assert_eq!(state.selected, 0);
}

#[test]
fn open_concerns_count_undelivered_warnings_and_criticals() {
    let mut state = PeerState::default();
    state.upsert(review("info"));
    state.reviews[0].severity = Severity::Info;
    state.upsert(review("warn"));
    state.upsert(review("crit"));
    state.reviews[2].severity = Severity::Critical;
    let mut delivered = review("done");
    delivered.delivered = true;
    state.upsert(delivered);
    state.upsert(running("live"));
    assert_eq!(state.open_concerns(), 2);
    state.mark_delivered("warn");
    assert_eq!(state.open_concerns(), 1);
}

#[test]
fn dismiss_removes_a_terminal_review_and_keeps_a_running_one() {
    let mut state = PeerState::default();
    state.upsert(running("live"));
    state.upsert(review("done"));
    state.move_selection(false);
    assert_eq!(state.selected, 0);
    state.dismiss_selected();
    assert_eq!(
        state.reviews.len(),
        2,
        "a running review has no verdict yet"
    );
    state.move_selection(true);
    state.dismiss_selected();
    assert_eq!(state.reviews.len(), 1);
    assert_eq!(state.reviews[0].review_id, "live");
    assert_eq!(state.selected, 0, "the cursor clamps after a removal");
}

#[test]
fn toggle_cycles_closed_focused_and_back_through_unfocused() {
    let mut state = PeerState::default();
    state.toggle();
    assert!(state.open);
    assert!(state.focused);
    assert!(state.opened_once);
    state.toggle();
    assert!(!state.open);
    assert!(!state.focused);
    // A docked panel that lost focus (Esc, auto-dock, `/peer` announce) takes
    // focus back instead of closing, so the steer keys stay keyboard-reachable.
    state.toggle();
    assert!(state.open);
    assert!(state.focused);
    state.focused = false;
    state.toggle();
    assert!(state.open);
    assert!(state.focused, "Ctrl+P refocuses a docked panel");
    state.toggle();
    assert!(!state.open, "a second Ctrl+P on a focused panel closes it");
    assert!(!state.focused);
}

#[test]
fn escape_unfocuses_first_then_closes_a_docked_panel() {
    let mut state = PeerState {
        open: true,
        focused: true,
        visible: true,
        ..PeerState::default()
    };
    assert!(state.escape());
    assert!(!state.focused);
    assert!(state.open, "the first Esc only unfocuses");
    assert!(state.escape());
    assert!(!state.open, "the second Esc closes");
    assert!(!state.escape(), "a closed panel passes the key through");
    let mut dropped = PeerState {
        open: true,
        visible: false,
        ..PeerState::default()
    };
    assert!(!dropped.escape(), "an undocked column keeps the panel open");
    assert!(dropped.open);
}

#[test]
fn selection_keys_wrap_at_both_ends_and_pages_clamp() {
    let mut state = PeerState::default();
    for id in ["r1", "r2", "r3"] {
        state.upsert(review(id));
    }
    assert_eq!(state.selected, 2, "the cursor starts on the newest review");
    state.move_selection(true);
    assert_eq!(state.selected, 0, "Down wraps past the last review");
    state.move_selection(false);
    assert_eq!(state.selected, 2, "Up wraps past the first review");
    state.move_selection(false);
    assert_eq!(state.selected, 1);
    state.select_edge(true);
    assert_eq!(state.selected, 2);
    state.page_selection(false, 10);
    assert_eq!(state.selected, 0, "PageUp clamps at the first review");
    state.page_selection(true, 10);
    assert_eq!(state.selected, 2, "PageDown clamps at the last review");
}

#[test]
fn nav_key_maps_the_panel_keys() {
    let mut app = App::default();
    app.peer.upsert(review("r1"));
    app.peer.upsert(review("r2"));
    app.peer.move_selection(false);
    peer::nav_key(&mut app, &key(KeyCode::Char('j')));
    assert_eq!(app.peer.selected, 1);
    peer::nav_key(&mut app, &key(KeyCode::Char('k')));
    assert_eq!(app.peer.selected, 0);
    peer::nav_key(&mut app, &key(KeyCode::Up));
    assert_eq!(
        app.peer.selected, 1,
        "Up at the top wraps to the last review"
    );
    peer::nav_key(&mut app, &key(KeyCode::End));
    assert_eq!(app.peer.selected, 1);
    peer::nav_key(&mut app, &key(KeyCode::Home));
    assert_eq!(app.peer.selected, 0);
}

#[test]
fn the_first_critique_auto_docks_the_panel_once() {
    let mut app = App::default();
    peer::apply_review(
        &mut app,
        &json!({"reviewId": "r1", "status": "running", "sessionId": "s1"}),
    );
    assert!(app.peer.open, "the first critique docks the panel");
    assert!(!app.peer.focused, "an auto-dock never steals the keyboard");
    assert_eq!(app.peer.reviews.len(), 1);
    app.peer.open = false;
    app.peer.opened_once = false;
    peer::apply_review(
        &mut app,
        &json!({"reviewId": "r2", "status": "running", "sessionId": "s1"}),
    );
    assert!(app.peer.open, "a fresh session auto-docks again");
    assert_eq!(app.peer.reviews.len(), 2);
}

#[test]
fn a_deliberate_open_stops_the_auto_dock() {
    let mut app = App::default();
    app.peer.toggle();
    app.peer.open = false;
    peer::apply_review(
        &mut app,
        &json!({"reviewId": "r1", "status": "complete", "sessionId": "s1"}),
    );
    assert!(!app.peer.open, "once opened, reviews only accumulate");
    assert_eq!(app.peer.reviews.len(), 1);
}

#[test]
fn apply_review_ignores_payloads_without_an_id() {
    let mut app = App::default();
    peer::apply_review(&mut app, &json!({"status": "running"}));
    assert!(app.peer.reviews.is_empty());
    assert!(!app.peer.open);
}

#[test]
fn click_row_selects_and_focuses_the_hit_review() {
    let mut app = App::default();
    app.peer.upsert(review("r1"));
    app.peer.upsert(review("r2"));
    app.peer.hitmap = vec![(3, 5, 0), (6, 9, 1)];
    peer::click_row(&mut app, 7);
    assert_eq!(app.peer.selected, 1);
    assert!(app.peer.focused);
    peer::click_row(&mut app, 99);
    assert_eq!(
        app.peer.selected, 1,
        "a click outside the hitmap changes nothing"
    );
}

#[test]
fn config_text_reports_the_state() {
    let off = PeerConfig {
        enabled: false,
        ..PeerConfig::default()
    };
    assert!(vibe_rs::commands::peer::config_text(&off).contains("off"));
    let on = PeerConfig {
        runs: Vec::new(),
        enabled: true,
        model: Some("glm-5.3".to_owned()),
        auto_steer: false,
        max_reviews_per_turn: 10,
        auto_steer_severity: Severity::Critical,
    };
    let text = vibe_rs::commands::peer::config_text(&on);
    assert!(text.contains("glm-5.3"));
    assert!(text.contains("auto-steer off"));
    assert!(text.contains("10 reviews per turn"));
}

#[test]
fn peer_command_parse_empty_reads() {
    use vibe_rs::commands::peer::{parse, PeerArgs};
    assert_eq!(parse(""), Ok(PeerArgs::Read));
    assert_eq!(parse("   "), Ok(PeerArgs::Read));
}

#[test]
fn peer_command_parse_off_disables() {
    use vibe_rs::commands::peer::{parse, PeerArgs};
    assert_eq!(
        parse("off"),
        Ok(PeerArgs::Configure {
            model: None,
            disable: true,
            auto_steer: None,
        })
    );
}

#[test]
fn peer_command_parse_model_enables() {
    use vibe_rs::commands::peer::{parse, PeerArgs};
    assert_eq!(
        parse("beta"),
        Ok(PeerArgs::Configure {
            model: Some("beta".to_owned()),
            disable: false,
            auto_steer: None,
        })
    );
}

#[test]
fn peer_command_parse_steer_toggles_auto_steer() {
    use vibe_rs::commands::peer::{parse, PeerArgs};
    assert_eq!(
        parse("steer on"),
        Ok(PeerArgs::Configure {
            model: None,
            disable: false,
            auto_steer: Some(true),
        })
    );
    assert_eq!(
        parse("steer off"),
        Ok(PeerArgs::Configure {
            model: None,
            disable: false,
            auto_steer: Some(false),
        })
    );
}

#[test]
fn peer_command_parse_model_and_steer_combine() {
    use vibe_rs::commands::peer::{parse, PeerArgs};
    assert_eq!(
        parse("beta steer on"),
        Ok(PeerArgs::Configure {
            model: Some("beta".to_owned()),
            disable: false,
            auto_steer: Some(true),
        })
    );
    assert_eq!(
        parse("steer off beta"),
        Ok(PeerArgs::Configure {
            model: Some("beta".to_owned()),
            disable: false,
            auto_steer: Some(false),
        })
    );
}

#[test]
fn peer_command_parse_rejects_bad_usage() {
    use vibe_rs::commands::peer::parse;
    assert!(parse("steer").is_err());
    assert!(parse("steer maybe").is_err());
    assert!(parse("steer on off").is_err());
    assert!(parse("beta gamma").is_err());
    assert!(parse("beta off").is_err());
    assert!(parse("off beta").is_err());
}

#[test]
fn run_params_parse_every_field() {
    let parsed = peer::PeerRun::from_value(&json!({
        "runId": "run-1",
        "kind": "agent",
        "prompt": "sweep of a mesh",
        "status": "complete",
        "steerable": true,
        "delivered": false,
        "text": "report",
        "error": null,
    }))
    .expect("a full payload parses");
    assert_eq!(parsed.run_id, "run-1");
    assert_eq!(parsed.kind, peer::RunKind::Agent);
    assert_eq!(parsed.prompt, "sweep of a mesh");
    assert_eq!(parsed.status, peer::RunStatus::Complete);
    assert!(parsed.steerable && !parsed.delivered);
    assert_eq!(parsed.text, "report");
}

#[test]
fn peer_slash_parse_handles_run_agent_stop() {
    use vibe_rs::commands::peer::{parse, PeerArgs};
    assert_eq!(
        parse("run independent critical review"),
        Ok(PeerArgs::Run {
            prompt: "independent critical review".to_owned()
        })
    );
    assert_eq!(
        parse("agent separate research about sweep of a mesh"),
        Ok(PeerArgs::Agent {
            prompt: "separate research about sweep of a mesh".to_owned()
        })
    );
    assert_eq!(parse("stop"), Ok(PeerArgs::Stop { run_id: None }));
    assert_eq!(
        parse("stop run-3"),
        Ok(PeerArgs::Stop {
            run_id: Some("run-3".to_owned())
        })
    );
    // Bare subcommand words are usage errors, not model aliases.
    assert!(parse("run").is_err());
    assert!(parse("agent").is_err());
    // Existing forms still parse.
    assert_eq!(parse(""), Ok(PeerArgs::Read));
    assert_eq!(
        parse("off"),
        Ok(PeerArgs::Configure {
            model: None,
            disable: true,
            auto_steer: None
        })
    );
    assert_eq!(
        parse("beta steer on"),
        Ok(PeerArgs::Configure {
            model: Some("beta".to_owned()),
            disable: false,
            auto_steer: Some(true)
        })
    );
}

#[test]
fn panel_cursor_spans_reviews_then_runs() {
    let mut state = PeerState::default();
    state.upsert(review("r1"));
    state.upsert(review("r2"));
    state.upsert_run(
        peer::PeerRun::from_value(&json!({
            "runId": "run-1", "kind": "run", "prompt": "p", "status": "running",
        }))
        .unwrap(),
    );
    assert_eq!(state.entry_count(), 3);
    state.select_edge(true);
    assert!(state.selected_run().is_some());
    state.select_edge(false);
    assert!(state.selected_review().is_some());
    state.move_selection(true);
    state.move_selection(true);
    assert_eq!(state.selected, 2);
    assert!(state.selected_run().is_some());
}
