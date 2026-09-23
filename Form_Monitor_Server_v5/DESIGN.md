# Sentleaf visual identity

Sentleaf is the public-facing name of this service. The product follows a form submission through to its matching Gmail Inbox message. The leaf/document mark combines a folded-looking page silhouette with a confirmation check.

The interface uses warm off-white surfaces, forest green actions, restrained delivery status colors, Instrument Sans, and occasional Georgia italic headings. Fonts are hosted with the application; their license is in `dashboard_assets/FONT_LICENSE.txt`. `logo.svg` and `favicon.svg` are scalable originals. Shared tokens and controls live in `brand.css`.

The landing page, sign-in/register page, monitor list, monitor detail, history, notifications, connections, backup controls, and three-step form setup share this identity. The homepage's interactive receipt and product illustration are explicitly labeled examples. Workspace counts, status strips, results, connections, and activity use API state.

Keep operational identifiers stable: repository and app directory names, environment variables, data paths, cookie names, backup format, OAuth callback paths, and test-message references still use their established names. Existing monitor names belong to users. Alert sender display names and subjects use Sentleaf.

## Validation

- Existing server and backup suite: 17 tests pass.
- All new public assets return 200; unauthenticated workspace access still redirects to login.
- DOM checks cover populated/empty workspaces, history filtering while focused, schedule start/pause, running a check, connection details, owner backup visibility, three-step monitor creation, mobile navigation controls, the homepage example, and registration/password controls.
- CSS parses and all three JavaScript entry points pass syntax checks.
- Responsive layouts are specified for desktop, tablet, and narrow screens, with reduced-motion support and visible keyboard focus.

Local synthetic QA fixtures are not part of the application or production routes.

## Subscription release (September 2026)

Refined warm-paper and evergreen direction, a new form reliability headline,
launch pricing from the server catalog, and Plan & billing in the workspace.
`refresh.css` contains the shared visual refresh; `plans.css` and `plans.js`
serve the same two plans on the homepage and in the workspace. Payment
unavailability and test mode are shown explicitly. Existing beta access and
owner access are preserved while billing is off.

See `BILLING_SETUP.md` for Stripe activation and the remaining public-release
requirements. Test fixtures and local previews are never published.
