"""core/cloudflare — Challenge-solving integrations for Muse 3.0.

Tier-3 of the fetch strategy: when plain HTTP (tier 1) and the stealth
browser backend (tier 2) are both blocked by a Cloudflare challenge, a
FlareSolverr instance solves the challenge inside *its* browser and this
package transports the solved cookies/user-agent back.

Session-alignment rule: solved cookies are only valid when replayed with
the exact user-agent (and ideally the same egress IP) FlareSolverr used.
See README.md for the full rules and the TLS/JA3 caveat.
"""

from core.cloudflare.flaresolverr import FlareSolverrClient

__all__ = ["FlareSolverrClient"]
