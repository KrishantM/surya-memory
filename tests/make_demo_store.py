"""Build a small made-up memory for trying the app and taking screenshots."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from surya_memory.store import Store  # noqa: E402

root = Path(sys.argv[1])
store = Store(root, project="demo", host="laptop")
NOTES = [
    ("alex-profile", "Alex is a backend engineer who ships side projects at weekends", "user", "claude-code",
     "Backend engineer, mostly Python and Postgres.\n\nCurrently building [[harbour-app]]. Writes in plain English and dislikes long replies. See [[short-replies]]."),
    ("short-replies", "Keep replies short and lead with the answer", "feedback", "claude-code",
     "Lead with the result, then stop.\n\n**Why:** long replies get skimmed.\n\n**How to apply:** three sentences unless asked for detail. Related: [[alex-profile]]."),
    ("tests-against-real-db", "Integration tests hit a real Postgres, never mocks", "feedback", "codex",
     "Tests must use a real database.\n\n**Why:** a mocked migration test once passed while the real migration failed.\n\nApplies to [[harbour-app]]."),
    ("harbour-app", "Harbour: booking app for small marinas, launching in March", "project", "claude-code",
     "## State\n\n- API on Fly, Postgres on Neon\n- Stripe wired, webhooks tested\n- Public launch planned for March\n\n## Decisions\n\n| Decision | Why |\n|---|---|\n| Postgres over DynamoDB | Reporting needs joins |\n| Server-rendered first | One person to maintain it |\n\nRelated: [[deploy-steps]], [[stripe-webhooks]], [[tests-against-real-db]]."),
    ("deploy-steps", "How Harbour deploys, and the one step that isn't automated", "project", "codex",
     "1. Merge to `main`.\n2. CI runs the suite against a real database.\n3. Run `fly deploy` by hand. Migrations run first.\n\nSee [[harbour-app]]."),
    ("stripe-webhooks", "Stripe webhook retries caused double bookings once", "project", "claude-code",
     "Handlers must be idempotent. Key on the event id.\n\nFound during the [[harbour-app]] beta. See also the [Stripe docs](https://docs.stripe.com/webhooks)."),
    ("design-tokens", "Where Harbour's colours and spacing are defined", "reference", "cursor",
     "All tokens live in `app/styles/tokens.css`. Don't add raw hex values to components."),
]
for name, description, kind, source, body in NOTES:
    store.remember(name, description, body, type=kind, source=source)
store.amend("deploy-steps", old="Run `fly deploy` by hand.", new="Run `fly deploy` by hand from the release branch.", source="codex")
store.mark_reviewed()
store.remember("marina-pricing", "Marinas pay per berth per month, no booking fee", "Decided after talking to six marinas. Part of [[harbour-app]].", type="project", source="codex")
store.amend("harbour-app", old="- Public launch planned for March", new="- Public launch moved to April", source="claude-code")
subprocess.run(["git", "-C", str(root), "log", "--oneline"], check=True)
