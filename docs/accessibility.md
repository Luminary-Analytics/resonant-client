# Accessibility conformance report (draft)

> **Draft, self-assessed. Not independently verified.** This follows the
> ITI VPAT® 2.5 format (WCAG edition) for WCAG 2.1 Levels A and AA. Luminary
> wrote it from its own checks on September 25, 2026. It hasn't been reviewed
> by an accessibility specialist, and hasn't been tested with screen readers.
> Buyers who need an independently audited report should ask Luminary.

## Products

- **Lumi**, the desktop app (the same interface in the Windows and macOS
  windows and in a browser opened from the app), version 0.19.2 development
  build.
- **Lumi Cloud**, the web portal, as of the same date.

## How it was evaluated

- **Automated checks in a real browser**, run on the rendered pages in the
  light and dark themes. Each theme was rendered from load, because switching
  themes in place leaves transitions half-done.
  - Every button, link and form field has an accessible name or label.
  - Duplicate ids, heading order, page language and a main landmark.
  - Text contrast against the actual composited background, with the
    WCAG 1.4.3 thresholds.
  - Coverage in the app: the main view, all 25 Settings pages, and the command
    palette, model picker, permission menu and Timeline (without checkpoints
    in it). In the portal: 21 pages, signed in as an owner.
- **Keyboard checks with real key presses**: tab order through the app's
  titlebar, sidebar and composer; visible focus; opening, moving through and
  closing the command palette, model picker, permission menu and Timeline;
  focus returning to what opened them; the skip control.
- **Added later the same day**: a run's Trace dialog and saved-file viewer
  (opened from a run's work details).
  - Text contrast was measured in both themes, including an error row and
    the striped rows. The lowest was 5.89:1, in the light theme.
  - They were used from the keyboard. Saving and "Show more" keep focus on
    their button, and Escape closes the viewer, then Trace, returning focus
    each time.

## Fixed during this review

- **Contrast in the app.** The dark theme's secondary text reached only
  3.0:1 on some surfaces, and the light theme's 3.7:1. Every text color token
  now reaches 4.5:1 on every surface in both themes, hover included.
- **The permission-mode menu couldn't be used from the keyboard.** Its
  options were plain elements. It's now a menu of radio items: Enter, Space
  or the arrow keys open it on the current mode, the arrows, Home and End
  move, and Escape closes it and returns focus. The toggle says whether it's
  open and the options say which is chosen.
- **Focus** on the command-palette button was only a faint border change,
  and on the composer's model and reasoning menus only a text color change.
  They now have focus rings.
- **A daily budget field** in Usage & cost had no label.
- **Bypassing blocks.** A "Skip to the message box" control (or "Skip to the
  Settings page") is now the first thing in the app's tab order.
- **Page titles.** The app's title names the screen: "Settings · Lumi".

**Dictation, September 25, 2026.** The microphone button beside the message
box worked only while held down with a mouse or a finger. Now:

- Space on the button dictates while held, and Enter (or a quick press)
  starts and stops it. Ctrl+Shift+Space does the same from anywhere in the
  conversation, and Escape cancels.
- The button says whether it's on (`aria-pressed`) and has a focus ring.
- A status line under the message box announces listening, transcribing and
  errors.

These were checked with real key presses in a browser, with a generated tone
as the microphone and a local fake transcription service supplying the
words. The microphone
button's shortcut is in the shortcuts list (Ctrl+/). That list named Alt+2,
Alt+3 and Alt+4 views that don't exist; it now matches the app.

**Correction, September 25, 2026.** The contrast check above skipped colours
Chrome reports as `color(srgb …)`, which is what `color-mix()` produces. That
missed the portal's status chips in the light theme: green chips such as
"Active", "Standard" and "Subscribed" were at 3.88:1 on their tint, and orange
ones at 3.94:1. Lumi Cloud's PR #31 darkened the light theme's `--ok` and
`--warn`, to 5.35 and 5.41. The same check, now reading those colours, found
no other problem on the portal's Overview, Billing, Support, Security and API
keys pages.

## WCAG 2.1 report

Conformance levels: **Supports**, **Partially Supports**, **Does Not
Support**, **Not Applicable**, **Not Evaluated**.

### Level A

| Criterion | Lumi app | Lumi Cloud portal | Remarks |
| --- | --- | --- | --- |
| 1.1.1 Non-text Content | Supports | Supports | Icons that act as controls have names; decorative icons are hidden from assistive technology. The mascot animation is decorative. |
| 1.2.1–1.2.3 Time-based media | Not Applicable | Not Applicable | No audio or video content. |
| 1.3.1 Info and Relationships | Partially Supports | Supports | Headings, lists, tables with header cells and labelled fields. In the app, some status and detail text is visual grouping only. Not screen-reader tested. |
| 1.3.2 Meaningful Sequence | Supports | Supports | DOM order matches reading order. |
| 1.3.3 Sensory Characteristics | Supports | Supports | Instructions don't depend on shape, size or position alone. |
| 1.4.1 Use of Color | Partially Supports | Supports | Status in the app pairs color with text (for example "Approved · active"), but some chips and diff lines rely mostly on color. |
| 1.4.2 Audio Control | Not Applicable | Not Applicable | No sound plays on its own. |
| 2.1.1 Keyboard | Partially Supports | Supports | Settings, the composer, menus and dialogs work from the keyboard (the permission menu was fixed during this review). Not every panel was walked. Dragging to resize panels has no keyboard alternative. |
| 2.1.2 No Keyboard Trap | Supports | Supports | Dialogs close with Escape and return focus. |
| 2.1.4 Character Key Shortcuts | Supports | Not Applicable | App shortcuts use a modifier (Ctrl+K, Ctrl+N). Tab only accepts a suggestion inside the composer. |
| 2.2.1 Timing Adjustable | Supports | Partially Supports | Portal sign-in links last 15 minutes. An organization can end portal sessions after 8 hours to 7 days, which is a security limit. |
| 2.2.2 Pause, Stop, Hide | Supports | Supports | The optional mascot follows reduced motion. Progress indicators stop when the work does. |
| 2.3.1 Three Flashes | Supports | Supports | Nothing flashes. |
| 2.4.1 Bypass Blocks | Supports | Supports | The app has the skip control added during this review; the portal has a skip link and landmarks. |
| 2.4.2 Page Titled | Supports | Supports | The portal titles each page. The app names its main screen and Settings. |
| 2.4.3 Focus Order | Supports | Supports | The app was checked by tabbing through its main view. Portal pages are server-rendered in reading order and weren't walked with the keyboard. |
| 2.4.4 Link Purpose (In Context) | Supports | Supports | |
| 3.1.1 Language of Page | Supports | Supports | `lang="en"`. |
| 3.2.1 On Focus | Supports | Supports | |
| 3.2.2 On Input | Supports | Partially Supports | The portal's organization and workspace pickers switch as soon as a choice is made (a Switch button appears without scripts). |
| 3.3.1 Error Identification | Supports | Supports | Errors are shown in text; the portal uses alerts. |
| 3.3.2 Labels or Instructions | Supports | Supports | Every visible field has a label in the checked views. |
| 4.1.1 Parsing | Supports | Supports | No duplicate ids in the checked views. |
| 4.1.2 Name, Role, Value | Partially Supports | Supports | Names on all controls, and states on the menus fixed here. Custom controls elsewhere in the app haven't been screen-reader tested. |

### Level AA

| Criterion | Lumi app | Lumi Cloud portal | Remarks |
| --- | --- | --- | --- |
| 1.2.4–1.2.5 Captions and audio description | Not Applicable | Not Applicable | No media. |
| 1.3.4 Orientation | Supports | Supports | Neither locks orientation. |
| 1.3.5 Identify Input Purpose | Not Evaluated | Partially Supports | The portal's billing address fields have `autocomplete`. Others not checked. |
| 1.4.3 Contrast (Minimum) | Supports | Supports | Measured in both themes after the fixes above, including the portal's status chips (see the correction). |
| 1.4.4 Resize Text | Partially Supports | Supports | The app's text size setting goes from 12 to 15 px, and browser zoom works in the browser view. Behavior at 200% in the desktop window wasn't checked. |
| 1.4.5 Images of Text | Supports | Supports | |
| 1.4.10 Reflow | Partially Supports | Partially Supports | Both have layouts for narrow windows. Neither was checked at 320 px. |
| 1.4.11 Non-text Contrast | Not Evaluated | Not Evaluated | Focus rings use the accent color. Borders and icons weren't measured. |
| 1.4.12 Text Spacing | Not Evaluated | Not Evaluated | |
| 1.4.13 Content on Hover or Focus | Partially Supports | Supports | The app uses native tooltips (`title`) and some hover-only hints. |
| 2.4.5 Multiple Ways | Supports | Supports | The app has Settings search, the command palette and navigation. The portal has navigation on every page. |
| 2.4.6 Headings and Labels | Supports | Supports | |
| 2.4.7 Focus Visible | Supports | Supports | The app was checked by tabbing through its main view, and the command-palette button and the composer's model and reasoning menus were fixed. The portal draws a focus outline on every control (`:focus-visible`); it wasn't walked with the keyboard. |
| 3.1.2 Language of Parts | Not Applicable | Not Applicable | Interface text is English; model output isn't marked up by language. |
| 3.2.3 Consistent Navigation | Supports | Supports | |
| 3.2.4 Consistent Identification | Supports | Supports | |
| 3.3.3 Error Suggestion | Supports | Supports | Errors say how to fix them. |
| 3.3.4 Error Prevention (Legal, Financial, Data) | Supports | Supports | Removals and approvals ask first; the portal confirms deletes. |
| 4.1.3 Status Messages | Partially Supports | Supports | Notices and statuses use status roles. The app's streaming answer and live activity haven't been screen-reader tested. |

## What's next

- Screen reader testing: NVDA and JAWS on Windows, VoiceOver on macOS.
- Non-text contrast (1.4.11) and text spacing (1.4.12).
- A keyboard alternative for resizing panels.
- The portal's pickers (3.2.2).
- An independent audit before publishing a VPAT to customers.
