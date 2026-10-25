//! The rubber-duck peer review panel: its state, the `peer/review` reducer,
//! and the keys it owns while focused.

use std::collections::VecDeque;

use crossterm::event::{KeyCode, KeyEvent};
use serde_json::Value;

use crate::app::App;

/// Reviews retained in the panel; past this the oldest drop (ADR 0016 bound).
pub const MAX_REVIEWS: usize = 100;

/// Page keys jump about one panel of reviews.
const PAGE_STEP: usize = 5;

/// The verdict severity, parsed off the wire.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Severity {
    #[default]
    Info,
    Warning,
    Critical,
}

impl Severity {
    pub fn from_wire(text: &str) -> Self {
        match text {
            "warning" => Self::Warning,
            "critical" => Self::Critical,
            _ => Self::Info,
        }
    }

    /// At or above `Warning` counts as an open concern until delivered.
    pub fn is_concern(self) -> bool {
        !matches!(self, Self::Info)
    }
}

/// The severity's wire label.
pub fn severity_label(severity: Severity) -> &'static str {
    match severity {
        Severity::Info => "info",
        Severity::Warning => "warning",
        Severity::Critical => "critical",
    }
}

/// The review lifecycle status, parsed off the wire.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ReviewStatus {
    #[default]
    Running,
    Complete,
    Failed,
}

impl ReviewStatus {
    fn from_wire(text: &str) -> Self {
        match text {
            "complete" => Self::Complete,
            "failed" => Self::Failed,
            _ => Self::Running,
        }
    }
}

/// One `peer/review` notification as the panel keeps it.
#[derive(Clone, Debug)]
pub struct PeerReview {
    pub review_id: String,
    pub turn_id: String,
    pub status: ReviewStatus,
    pub severity: Severity,
    pub steerable: bool,
    pub delivered: bool,
    pub text: String,
    pub error: Option<String>,
}

impl PeerReview {
    /// Parse the notification params; `None` when the review id is missing.
    pub fn from_value(params: &Value) -> Option<Self> {
        let review_id = params.get("reviewId")?.as_str()?.to_owned();
        let text_of = |name: &str| {
            params
                .get(name)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned()
        };
        Some(Self {
            review_id,
            turn_id: text_of("turnId"),
            status: ReviewStatus::from_wire(
                params
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            ),
            severity: Severity::from_wire(
                params
                    .get("severity")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            ),
            steerable: params
                .get("steerable")
                .and_then(Value::as_bool)
                .unwrap_or_default(),
            delivered: params
                .get("delivered")
                .and_then(Value::as_bool)
                .unwrap_or_default(),
            text: text_of("text"),
            error: params
                .get("error")
                .and_then(Value::as_str)
                .map(str::to_owned),
        })
    }
}

/// The `peer/read` / `peer/configure` view the panel header renders.
#[derive(Clone, Debug, Default)]
pub struct PeerConfig {
    pub enabled: bool,
    pub model: Option<String>,
    pub auto_steer: bool,
    pub max_reviews_per_turn: u32,
    pub auto_steer_severity: Severity,
}

impl PeerConfig {
    /// Parse the response leniently; `None` only when `enabled` is absent.
    pub fn from_value(value: &Value) -> Option<Self> {
        let enabled = value.get("enabled")?.as_bool()?;
        Some(Self {
            enabled,
            model: value
                .get("model")
                .and_then(Value::as_str)
                .map(str::to_owned),
            auto_steer: value
                .get("autoSteer")
                .and_then(Value::as_bool)
                .unwrap_or_default(),
            max_reviews_per_turn: value
                .get("maxReviewsPerTurn")
                .and_then(Value::as_u64)
                .unwrap_or_default() as u32,
            auto_steer_severity: Severity::from_wire(
                value
                    .get("autoSteerSeverity")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            ),
        })
    }
}

/// The panel's client-side state: dock, focus, cursor, config and reviews.
#[derive(Default)]
pub struct PeerState {
    /// While set the panel docks on the right of the chat area.
    pub open: bool,
    /// While set the panel owns navigation, steer and dismiss.
    pub focused: bool,
    /// Whether the last frame actually docked it; narrow terminals drop the column.
    pub visible: bool,
    /// Top line offset of the panel list, clamped at paint time.
    pub scroll: usize,
    /// The selected review, an index into `reviews`.
    pub selected: usize,
    /// The last config read, if any.
    pub config: Option<PeerConfig>,
    /// Reviews oldest-first, bounded by `MAX_REVIEWS`.
    pub reviews: VecDeque<PeerReview>,
    /// Row hitmap painted last frame: `(top, bottom, review index)` per review.
    pub hitmap: Vec<(u16, u16, usize)>,
    /// Set once the user opens the panel; the first critique auto-docks it
    /// only before that, so a deliberate close is never overridden.
    pub opened_once: bool,
}

impl PeerState {
    /// Ctrl+P: a closed panel docks and takes focus, a docked panel that lost
    /// focus takes it back, and a focused panel undocks. Esc backs out one
    /// step at a time, so every docked state is keyboard-reachable.
    pub fn toggle(&mut self) {
        if !self.open {
            self.open = true;
            self.opened_once = true;
            self.focused = true;
        } else if !self.focused {
            self.focused = true;
        } else {
            self.open = false;
            self.focused = false;
        }
    }

    /// The `/peer` answer lands: open without taking the keyboard.
    pub fn open_unfocused(&mut self) {
        self.open = true;
        self.opened_once = true;
        self.focused = false;
    }

    /// Esc: unfocus first, then undock; true when the panel consumed the key.
    pub fn escape(&mut self) -> bool {
        if self.focused {
            self.focused = false;
            return true;
        }
        let docked = self.open && self.visible;
        self.open &= !docked;
        self.focused = false;
        docked
    }

    pub fn set_config(&mut self, config: PeerConfig) {
        self.config = Some(config);
    }

    /// Upsert one review by id: the running notification lands first, the
    /// complete/failed one updates it in place.
    pub fn upsert(&mut self, review: PeerReview) {
        if let Some(existing) = self
            .reviews
            .iter_mut()
            .find(|stored| stored.review_id == review.review_id)
        {
            *existing = review;
            return;
        }
        // A cursor resting on the newest review follows the feed.
        let follow =
            self.reviews.is_empty() || self.selected >= self.reviews.len().saturating_sub(1);
        self.reviews.push_back(review);
        if self.reviews.len() > MAX_REVIEWS {
            self.reviews.pop_front();
        }
        if follow {
            self.selected = self.reviews.len() - 1;
        }
        self.clamp_selection();
    }

    /// Open concerns: complete critiques at or above warning that never landed.
    pub fn open_concerns(&self) -> usize {
        self.reviews
            .iter()
            .filter(|review| {
                review.status == ReviewStatus::Complete
                    && review.severity.is_concern()
                    && !review.delivered
            })
            .count()
    }

    pub fn selected_review(&self) -> Option<&PeerReview> {
        self.reviews.get(self.selected)
    }

    /// Mark a review delivered after a manual steer landed.
    pub fn mark_delivered(&mut self, review_id: &str) {
        if let Some(review) = self
            .reviews
            .iter_mut()
            .find(|stored| stored.review_id == review_id)
        {
            review.delivered = true;
        }
    }

    /// `d`: drop the selected review locally; a running review has no verdict yet.
    pub fn dismiss_selected(&mut self) {
        let Some(review) = self.reviews.get(self.selected) else {
            return;
        };
        if review.status == ReviewStatus::Running {
            return;
        }
        self.reviews.remove(self.selected);
        self.clamp_selection();
    }

    /// Keep the cursor on a review.
    pub fn clamp_selection(&mut self) {
        self.selected = self.selected.min(self.reviews.len().saturating_sub(1));
    }

    /// ↑↓/jk move the cursor, wrapping at both ends (the panel is not
    /// composer-attached); PageUp/PageDown/Home/End clamp.
    pub fn move_selection(&mut self, down: bool) {
        if self.reviews.is_empty() {
            return;
        }
        self.selected = crate::list_nav::wrap(self.selected, self.reviews.len(), down);
    }

    pub fn select_edge(&mut self, last: bool) {
        self.selected = if self.reviews.is_empty() || !last {
            0
        } else {
            self.reviews.len() - 1
        };
    }

    pub fn page_selection(&mut self, down: bool, page: usize) {
        self.clamp_selection();
        if down {
            self.selected = (self.selected + page).min(self.reviews.len().saturating_sub(1));
        } else {
            self.selected = self.selected.saturating_sub(page);
        }
    }
}

/// Reduce one `peer/review` notification into the panel. The first critique
/// docks the panel so the duck's output always reaches the UI (design §7.1);
/// once the user has opened it themselves, later reviews only accumulate.
pub fn apply_review(app: &mut App, params: &Value) {
    let Some(review) = PeerReview::from_value(params) else {
        return;
    };
    if !app.peer.open && !app.peer.opened_once {
        app.peer.open = true;
        app.peer.opened_once = true;
    }
    app.peer.upsert(review);
}

/// A click in the panel: focus it and select the review under the pointer.
pub fn click_row(app: &mut App, row: u16) {
    let Some((_, _, index)) = app
        .peer
        .hitmap
        .iter()
        .find(|(top, bottom, _)| row >= *top && row < *bottom)
    else {
        return;
    };
    app.peer.selected = *index;
    app.peer.focused = true;
    app.set_app_focus(false);
    crate::selection::blur_composer(app);
}

/// The navigation keys the panel owns while focused; Enter steers and `d`
/// dismisses, handled by the caller because they need the client.
pub fn nav_key(app: &mut App, key: &KeyEvent) {
    match key.code {
        KeyCode::Up | KeyCode::Char('k') => app.peer.move_selection(false),
        KeyCode::Down | KeyCode::Char('j') => app.peer.move_selection(true),
        KeyCode::Home => app.peer.select_edge(false),
        KeyCode::End => app.peer.select_edge(true),
        KeyCode::PageUp => app.peer.page_selection(false, PAGE_STEP),
        KeyCode::PageDown => app.peer.page_selection(true, PAGE_STEP),
        _ => {}
    }
}
