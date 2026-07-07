---
version: alpha
name: MoneyPrinterTurbo UI
description: Practical visual system for the MoneyPrinterTurbo Streamlit app.
colors:
  background: "#F7F8FA"
  surface: "#FFFFFF"
  text: "#172033"
  muted: "#667085"
  border: "#D0D5DD"
  primary: "#2563EB"
  primaryHover: "#1D4ED8"
  success: "#16A34A"
  warning: "#D97706"
  danger: "#DC2626"
  info: "#0891B2"
typography:
  h1:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: 2rem
    fontWeight: 700
    lineHeight: 1.2
  h2:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: 1.25rem
    fontWeight: 650
    lineHeight: 1.3
  body:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: 1rem
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: 0.875rem
    fontWeight: 600
    lineHeight: 1.35
rounded:
  sm: 4px
  md: 8px
  lg: 10px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
components:
  primaryButton:
    backgroundColor: "{colors.primary}"
    textColor: "#FFFFFF"
    hoverBackgroundColor: "{colors.primaryHover}"
    borderRadius: "{rounded.md}"
  panel:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.border}"
    borderRadius: "{rounded.md}"
  statusSuccess:
    color: "{colors.success}"
  statusWarning:
    color: "{colors.warning}"
  statusDanger:
    color: "{colors.danger}"
---

## Overview

MoneyPrinterTurbo is a production tool for generating videos, not a marketing
site. The interface should feel calm, capable, and direct. Prefer dense but
organized controls, clear labels, predictable grouping, and visible feedback.

Do not introduce decorative hero sections, gradient-heavy pages, oversized
cards, or ornamental backgrounds. The first screen should remain useful for
configuring and generating videos.

## Colors

Use neutral surfaces for configuration-heavy screens. Use blue only for primary
actions and selected states. Use green, amber, and red only for status feedback.

Avoid single-hue themes. A screen should not become all-blue, all-purple,
beige-heavy, or dark-slate-heavy unless the existing app has already committed
to that local pattern.

## Typography

Use system fonts and compact headings. Reserve large type for the application
title only. Labels inside panels should be short and scannable.

Do not scale text with viewport width. Keep letter spacing at `0`.

## Layout

Keep Streamlit panels functional. Group related settings together, and avoid
nesting cards inside cards. Use stable widths and predictable spacing so controls
do not jump when selections change.

For video generation workflows, prioritize:

- script input and topic controls
- source/material selection
- voice/audio controls
- subtitle and output settings
- generation status and final preview

## Components

Use normal controls for the job: checkboxes for binary settings, multiselects
for source lists, sliders or selectboxes for numeric choices, tabs for grouped
API key management, and file uploaders for local media.

Buttons should use concise action text. Status messages should say what happened
and what the user can do next.

## Accessibility

Maintain WCAG AA contrast for text and button labels. Do not rely on color alone
for errors or success states; include clear text. Long translated labels must
wrap cleanly and must not overlap neighboring controls.

## Motion And Media

Media previews should show the actual generated or uploaded media whenever
possible. Avoid decorative placeholder visuals. If adding animations, keep them
subtle and never block the generation workflow.
