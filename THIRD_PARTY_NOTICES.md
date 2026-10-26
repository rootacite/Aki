# Third-Party Notices

This project is distributed under the GNU General Public License, version 3
or later (`GPL-3.0-or-later`). The following third-party components retain
their own licenses.

## Letta Code

- Component: `vendor/letta-code`
- Upstream: <https://github.com/letta-ai/letta-code>
- Version: `0.34.24`
- License: Apache License 2.0
- Copyright: 2025, Letta authors
- License text: `vendor/letta-code/LICENSE`

Apache-2.0 is compatible with GPL-3.0-or-later. The submodule is kept
unmodified; Aki-specific changes are applied from `patches/letta-code/` to a
separate build tree.

### Letta Brand Assets Exclusion

The upstream Letta Code license excludes the Letta name, Letta Code name,
logo, wordmark, images, and ASCII art from the Apache-2.0 grant. Those assets
remain copyrighted by Letta, Inc. and are not redistributed in Aki's runtime
package. This project does not grant any rights to use Letta branding.

## Bun

- Component: Bun `1.3.14` (build toolchain only)
- Upstream: <https://github.com/oven-sh/bun>
- License: MIT

## Python Dependencies

The Python gateway uses only the Python standard library unless separately
installed by the user; separately installed packages retain their own
licenses.
