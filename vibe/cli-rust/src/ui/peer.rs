//! The peer review panel: a right-docked split showing the duck's critiques.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Padding, Paragraph};
use ratatui::Frame;

use super::super::mouse::{self, MouseTarget};
use super::theme;
use crate::app::App;
use crate::hints;
use crate::peer::{PeerReview, PeerState, ReviewStatus, Severity};

/// Below this the docked column would leave the chat too narrow to read.
const MIN_DOCK_WIDTH: u16 = 90;

/// The focused panel's hint row; clipped on narrow docks.
const PANEL_HINTS: &[hints::Hint] = &[hints::NAVIGATE, hints::STEER, hints::DISMISS, hints::CLOSE];

/// Carve the panel's column off the chat area, or leave the area whole.
pub fn split(app: &App, area: Rect) -> (Rect, Option<Rect>) {
    if !app.peer.open || area.width < MIN_DOCK_WIDTH {
        return (area, None);
    }
    let panel_width = (area.width / 3).clamp(32, 64);
    let main = Rect {
        width: area.width - panel_width,
        ..area
    };
    let panel = Rect {
        x: main.right(),
        width: panel_width,
        ..area
    };
    (main, Some(panel))
}

/// The peer panel: config header, the review list with the cursor, key hints.
pub fn draw_panel(app: &mut App, f: &mut Frame, area: Rect) {
    if area.width < 6 || area.height < 5 {
        return;
    }
    mouse::register_region(app, area, MouseTarget::PeerPanel);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(theme::text(theme::primary()))
        .style(Style::default().bg(theme::surface()))
        .title(Span::styled(
            title_text(&app.peer),
            theme::text(theme::primary()).add_modifier(Modifier::BOLD),
        ))
        .padding(Padding::horizontal(1));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width < 1 || inner.height < 3 {
        return;
    }
    let header_area = Rect { height: 1, ..inner };
    let list_area = Rect {
        y: inner.y + 1,
        height: inner.height - 2,
        ..inner
    };
    let hint_area = Rect {
        y: list_area.bottom(),
        height: 1,
        ..inner
    };
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            header_text(&app.peer),
            theme::muted_style(),
        ))),
        header_area,
    );
    draw_list(app, f, list_area);
    if app.peer.focused {
        super::hint_line::draw_clipped(f, inner.x, hint_area.y, inner.width, PANEL_HINTS);
    } else {
        f.render_widget(
            Paragraph::new(super::hint_line::line(&[hints::FOCUS]))
                .alignment(ratatui::layout::Alignment::Right),
            hint_area,
        );
    }
}

/// The panel title: the duck model and the open-concern count.
fn title_text(peer: &PeerState) -> String {
    let model = peer
        .config
        .as_ref()
        .and_then(|config| config.model.as_deref())
        .unwrap_or("off");
    format!(" Peer · {model} · {} open ", peer.open_concerns())
}

/// The header row under the title: what the duck runs with, or how to enable it.
fn header_text(peer: &PeerState) -> String {
    let Some(config) = peer.config.as_ref() else {
        return "off · /peer <model> to enable".to_owned();
    };
    if !config.enabled {
        return "off · /peer <model> to enable".to_owned();
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
        "duck {model} · {steer} · budget {}/turn",
        config.max_reviews_per_turn
    )
}

/// The review list: status line and wrapped text per review, the cursor's
/// review kept in view, and the row hitmap a click selects from.
fn draw_list(app: &mut App, f: &mut Frame, area: Rect) {
    if area.width < 1 || area.height < 1 {
        return;
    }
    let (rows, owners) = panel_rows(&app.peer, area.width as usize);
    let peer = &mut app.peer;
    let selected_row = owners
        .iter()
        .position(|review| *review == Some(peer.selected));
    // While focused, keep the selected review inside the viewport.
    if peer.focused {
        if let Some(first) = selected_row {
            let last = owners
                .iter()
                .rposition(|review| *review == Some(peer.selected))
                .unwrap_or(first);
            if first < peer.scroll {
                peer.scroll = first;
            }
            if last >= peer.scroll + area.height as usize {
                peer.scroll = last + 1 - area.height as usize;
            }
        }
    }
    let max_scroll = rows.len().saturating_sub(area.height as usize);
    peer.scroll = peer.scroll.min(max_scroll);
    peer.hitmap.clear();
    let top = peer.scroll;
    let mut lines: Vec<Line> = Vec::new();
    for (index, line) in rows.into_iter().enumerate() {
        let Some(offset) = index.checked_sub(top) else {
            continue;
        };
        if offset as u16 >= area.height {
            break;
        }
        if let Some(review) = owners.get(index).copied().flatten() {
            let screen_row = area.y + offset as u16;
            if let Some(last) = peer.hitmap.last_mut() {
                if last.2 == review && last.1 == screen_row {
                    last.1 = screen_row + 1;
                    lines.push(line);
                    continue;
                }
            }
            peer.hitmap.push((screen_row, screen_row + 1, review));
        }
        lines.push(line);
    }
    f.render_widget(Paragraph::new(lines), area);
    // The focused review's status line is the list-cursor bar.
    if peer.focused {
        if let Some(row) = selected_row {
            let offset = row.saturating_sub(top);
            if offset < area.height as usize {
                super::list_cursor::paint(
                    f,
                    Rect {
                        y: area.y + offset as u16,
                        height: 1,
                        ..area
                    },
                );
            }
        }
    }
}

/// The panel's rows and the review each row belongs to (`None` for blanks).
fn panel_rows(peer: &PeerState, width: usize) -> (Vec<Line<'static>>, Vec<Option<usize>>) {
    let mut rows = Vec::new();
    let mut owners = Vec::new();
    if peer.reviews.is_empty() {
        rows.push(Line::from(Span::styled(
            "No reviews yet",
            theme::muted_style(),
        )));
        owners.push(None);
        return (rows, owners);
    }
    for (index, review) in peer.reviews.iter().enumerate() {
        rows.push(status_line(review));
        owners.push(Some(index));
        let body = match review.status {
            ReviewStatus::Failed => review.error.as_deref().unwrap_or("failed"),
            _ => review.text.as_str(),
        };
        let style = body_style(review);
        let wrapped = crate::utils::text::wrap_hard(body, width.saturating_sub(2));
        if wrapped.is_empty() {
            rows.push(Line::default());
            owners.push(Some(index));
        }
        for line in wrapped {
            rows.push(Line::from(Span::styled(format!("  {line}"), style)));
            owners.push(Some(index));
        }
        rows.push(Line::default());
        owners.push(None);
    }
    (rows, owners)
}

/// One review's status line: lifecycle glyph, severity, delivery marker.
fn status_line(review: &PeerReview) -> Line<'static> {
    let spans = match review.status {
        ReviewStatus::Running => {
            vec![Span::styled("… reviewing", theme::muted_style())]
        }
        ReviewStatus::Failed => {
            vec![Span::styled("✗ failed", theme::text(theme::error()))]
        }
        ReviewStatus::Complete => {
            let severity = severity_style(review.severity);
            let mut spans = vec![
                Span::styled(severity_icon(review.severity), severity),
                Span::styled(
                    format!(" {}", crate::peer::severity_label(review.severity)),
                    severity,
                ),
            ];
            if review.delivered {
                spans.push(Span::styled(" · delivered", theme::text(theme::success())));
            } else if review.severity.is_concern() {
                spans.push(Span::styled(" · open", theme::muted_style()));
            }
            spans
        }
    };
    Line::from(spans)
}

/// The verdict text's color: plain while complete, muted once failed or running.
fn body_style(review: &PeerReview) -> Style {
    match review.status {
        ReviewStatus::Complete => theme::text(theme::foreground()),
        _ => theme::muted_style(),
    }
}

/// The severity's color: muted info, warning amber, critical red.
fn severity_style(severity: Severity) -> Style {
    match severity {
        Severity::Info => theme::muted_style(),
        Severity::Warning => theme::text(theme::warning()),
        Severity::Critical => theme::text(theme::error()),
    }
}

/// The severity's glyph.
fn severity_icon(severity: Severity) -> &'static str {
    match severity {
        Severity::Info => "○",
        Severity::Warning => "▲",
        Severity::Critical => "●",
    }
}
