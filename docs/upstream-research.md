# Upstream evaluation — 2026-11-03

Inspected source and licenses before implementation:

| Project / inspected commit | Findings | Suitability and licensing |
| --- | --- | --- |
| [mxmichels17/tum-mcp](https://github.com/mxmichels17/tum-mcp/tree/fba48fed625756fda1f056c13103b4d71681267a) | `src/platforms/moodle.ts` uses a Shibboleth session, extracts `sesskey`, and POSTs JSON to `/lib/ajax/service.php`. Course discovery uses `core_course_get_enrolled_courses_by_timeline_classification`. `src/platforms/moodle-web.ts` and `src/collector/units/moodle.ts` inspect normal course/resource/folder pages and revision-bearing `pluginfile.php` links. | MIT; copying substantial code would require preserving its copyright/license notice. Its credential-driven SSO implementation is unsuitable for this project's no-password-collection and MFA requirements. Its README reports mobile web services disabled at TUM, but also lists outstanding live checks; these claims are upstream observations, not independent verification here. |
| [C0D3D3V/Moodle-DL](https://github.com/C0D3D3V/Moodle-DL/tree/d88b2343f4a54a21404335648bd0c459853fa587) | README explicitly requires Moodle mobile API access. `moodle_dl/moodle/request_helper.py` uses REST tokens; `core_handler.py` retrieves courses/contents using web services. Has extensive change tracking and download handling. SSO support obtains API tokens, so SSO alone does not establish TUM compatibility. | GPL-3.0; incorporating GPL code into a distributed derivative would bring GPL obligations. A large, API-centric dependency is unsuitable for this small, session-based MVP. |

Decision: independent Python implementation, MIT licensed, with no copied or vendored code from either project. References inform protocol choices only. Dependency licenses remain their respective licenses (see installed package metadata). No claims of legal review.

## Authentication/resource retrieval evidence

The MVP uses Playwright only for a fresh visible browser login. The user operates TUM's actual login pages, including MFA or CAPTCHA. It does not fill/read password fields, intercept form submissions, automate SAML exchanges, or obtain REST/mobile tokens. After login, only cookies scoped to `www.moodle.tum.de` are handed to a same-origin HTTP client. A protected `/my/` request must contain a Moodle session key and authenticated logout marker before a session is saved.

Course discovery uses the session-enabled AJAX method above with explicit pagination. The request is sent with the current session's `sesskey`, never a configured token. AJAX denial or an unrecognized response fails explicitly rather than reporting zero courses. Standard resource/folder activities and section pages are read as HTML; `mod_resource`/`mod_folder` pluginfile URLs supply revision information. Embedded object/iframe links are supported when they target those same file URLs. External hosts and write/action links are not crawled.

A direct unauthenticated HTTPS check to `https://www.moodle.tum.de/` on 2026-11-03 failed at the cloud egress proxy (`CONNECT` HTTP 403). No TUM credentials or session were provided. **The live TUM SSO flow, current AJAX availability and current course-page markup were not independently verified.** No assumption of standard Moodle API token availability is made. Compatibility must be confirmed with the manual acceptance procedure in README before relying on scheduled runs.

Local synthetic fixtures test the selected contracts; they are not captured TUM pages and contain no private course data. A real Chromium fixture tests cookie handoff with all browser requests intercepted. Google Drive desktop upload completion, Windows DPAPI and Windows Task Scheduler need testing on Windows; Linux results do not establish them.
