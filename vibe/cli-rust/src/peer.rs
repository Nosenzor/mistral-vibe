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
    pub runs: Vec<PeerRun>,
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
            runs: value
                .get("runs")
                .and_then(Value::as_array)
                .map(|runs| runs.iter().filter_map(PeerRun::from_value).collect())
                .unwrap_or_default(),
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
    /// Direct duck runs oldest-first, bounded by `MAX_RUNS`.
    pub runs: VecDeque<PeerRun>,
    /// Row hitmap painted last frame: `(top, bottom, review index)` per review.
    pub hitmap: Vec<(u16, u16, usize)>,
    /// Row hitmap for runs: `(top, bottom, run index)` per run row.
    pub run_hitmap: Vec<(u16, u16, usize)>,
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
            self.entry_count() == 1 || self.selected >= self.entry_count().saturating_sub(1);
        self.reviews.push_back(review);
        if self.reviews.len() > MAX_REVIEWS {
            self.reviews.pop_front();
            if !follow {
                // The eviction shifts every index down; keep the cursor on
                // the same review.
                self.selected = self.selected.saturating_sub(1);
            }
        }
        if follow {
            self.selected = self.entry_count() - 1;
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

    /// The selected entry as a review or a run.
    pub fn selected_entry(&self) -> Option<SelectedEntry<'_>> {
        if let Some(review) = self.selected_review() {
            return Some(SelectedEntry::Review(review));
        }
        self.selected_run().map(SelectedEntry::Run)
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

    /// `d`: drop the selected entry locally when it is terminal; a running
    /// review or run is left for the caller to stop.
    pub fn dismiss_selected(&mut self) -> bool {
        match self.selected_entry() {
            Some(SelectedEntry::Review(review)) if review.status != ReviewStatus::Running => {
                self.reviews.remove(self.selected);
                self.clamp_selection();
                true
            }
            Some(SelectedEntry::Run(run)) if run.status.is_terminal() => {
                let index = self.selected - self.reviews.len();
                self.runs.remove(index);
                self.clamp_selection();
                true
            }
            _ => false,
        }
    }

    /// Upsert one run by id, bounded like reviews.
    pub fn upsert_run(&mut self, run: PeerRun) {
        if let Some(existing) = self
            .runs
            .iter_mut()
            .find(|stored| stored.run_id == run.run_id)
        {
            *existing = run;
            return;
        }
        self.runs.push_back(run);
        if self.runs.len() > MAX_RUNS {
            self.runs.pop_front();
            self.selected = self.selected.saturating_sub(1);
        }
        self.clamp_selection();
    }

    /// Total selectable entries: reviews first, then runs.
    pub fn entry_count(&self) -> usize {
        self.reviews.len() + self.runs.len()
    }

    /// The selected run, when the cursor is past the reviews.
    pub fn selected_run(&self) -> Option<&PeerRun> {
        self.selected
            .checked_sub(self.reviews.len())
            .and_then(|index| self.runs.get(index))
    }

    /// Mark a run delivered after a steer landed.
    pub fn mark_run_delivered(&mut self, run_id: &str) {
        if let Some(run) = self.runs.iter_mut().find(|stored| stored.run_id == run_id) {
            run.delivered = true;
        }
    }

    /// Keep the cursor on an entry.
    pub fn clamp_selection(&mut self) {
        self.selected = self.selected.min(self.entry_count().saturating_sub(1));
    }

    /// ↑↓/jk move the cursor, wrapping at both ends (the panel is not
    /// composer-attached); PageUp/PageDown/Home/End clamp.
    pub fn move_selection(&mut self, down: bool) {
        if self.entry_count() == 0 {
            return;
        }
        self.selected = crate::list_nav::wrap(self.selected, self.entry_count(), down);
    }

    pub fn select_edge(&mut self, last: bool) {
        self.selected = if self.entry_count() == 0 || !last {
            0
        } else {
            self.entry_count() - 1
        };
    }

    pub fn page_selection(&mut self, down: bool, page: usize) {
        self.clamp_selection();
        if down {
            self.selected = (self.selected + page).min(self.entry_count().saturating_sub(1));
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

/// Reduce one `peer/result` notification into the panel's run list.
pub fn apply_result(app: &mut App, params: &Value) {
    let Some(run) = PeerRun::from_value(params) else {
        return;
    };
    if !app.peer.open && !app.peer.opened_once {
        app.peer.open = true;
        app.peer.opened_once = true;
    }
    app.peer.upsert_run(run);
}

/// A click in the panel: focus it and select the review under the pointer.
pub fn click_row(app: &mut App, row: u16) {
    let review = app
        .peer
        .hitmap
        .iter()
        .find(|(top, bottom, _)| row >= *top && row < *bottom)
        .map(|(_, _, index)| *index);
    let run = app
        .peer
        .run_hitmap
        .iter()
        .find(|(top, bottom, _)| row >= *top && row < *bottom)
        .map(|(_, _, index)| app.peer.reviews.len() + *index);
    let Some(selected) = review.or(run) else {
        return;
    };
    app.peer.selected = selected;
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

/// The cursor's selected entry: a review or a run.
#[derive(Clone, Copy, Debug)]
pub enum SelectedEntry<'a> {
    Review(&'a PeerReview),
    Run(&'a PeerRun),
}

/// Runs (`/peer run` / `/peer agent`) retained in the panel, bounded like reviews.
pub const MAX_RUNS: usize = 50;

/// The run kind, parsed off the wire.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum RunKind {
    #[default]
    Run,
    Agent,
}

impl RunKind {
    fn from_wire(text: &str) -> Self {
        match text {
            "agent" => Self::Agent,
            _ => Self::Run,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Run => "run",
            Self::Agent => "agent",
        }
    }
}

/// The run lifecycle status, parsed off the wire.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum RunStatus {
    #[default]
    Running,
    Complete,
    Failed,
    Cancelled,
}

impl RunStatus {
    fn from_wire(text: &str) -> Self {
        match text {
            "complete" => Self::Complete,
            "failed" => Self::Failed,
            "cancelled" => Self::Cancelled,
            _ => Self::Running,
        }
    }

    pub fn is_terminal(self) -> bool {
        !matches!(self, Self::Running)
    }
}

/// One `peer/result` notification as the panel keeps it.
#[derive(Clone, Debug)]
pub struct PeerRun {
    pub run_id: String,
    pub kind: RunKind,
    pub prompt: String,
    pub status: RunStatus,
    pub steerable: bool,
    pub delivered: bool,
    pub text: String,
    pub error: Option<String>,
}

impl PeerRun {
    /// Parse the notification params; `None` when the run id is missing.
    pub fn from_value(params: &Value) -> Option<Self> {
        let run_id = params.get("runId")?.as_str()?.to_owned();
        let text_of = |name: &str| {
            params
                .get(name)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned()
        };
        Some(Self {
            run_id,
            kind: RunKind::from_wire(
                params
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            ),
            prompt: text_of("prompt"),
            status: RunStatus::from_wire(
                params
                    .get("status")
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
