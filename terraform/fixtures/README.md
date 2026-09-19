# Terraform fixtures

## What is in here

`deployed-at-10.tfstate` is a **test fixture**, not account state.

It is a hand-authored Terraform state file describing a hypothetical deployment of
the demo stack at `reserved_concurrent_executions = 10`. No AWS account was ever
involved in producing it, and none of the resources it names exist. The ARNs contain
the placeholder account id `000000000000` precisely so it cannot be mistaken for
real output.

## Why it exists

To generate the demo change as a genuine **update** diff rather than a create.

Terraform computes a plan by comparing configuration against prior state. With no
state, `terraform plan` produces `"actions": ["create"]` for everything, which does
not exercise the before/after attribute diff the parser is built around. Planning
against this fixture with `-refresh=false` produces:

```json
"actions": ["update"],
"before": { "reserved_concurrent_executions": 10 },
"after":  { "reserved_concurrent_executions": 100 }
```

That is real Terraform output, produced offline, at zero cost.

## Verification status

**Unverified.** The Terraform CLI is not installed in the environment where this
fixture was written, so it has not been proven that Terraform accepts this state as
valid input. AWS provider state carries a large attribute set, and Terraform may
reject or partially ignore a hand-authored file that omits attributes it expects.

Until `scripts/generate_plan.sh` has been run successfully at least once, treat this
file as a best-effort starting point rather than a working artifact.

The Phase 0 test suite does not depend on it. Tests run against
`backend/lambda/changeproof/fixtures/terraform_plan.json`, which is hand-authored to
Terraform's documented plan schema and is independently valid.

## How to verify it

1. Install the Terraform CLI (free, no AWS account required)
2. Run `bash scripts/generate_plan.sh`
3. If Terraform rejects the state, add the attributes it names and re-run

The script never applies anything, never refreshes state, and never contacts AWS.
It works on a copy, so this fixture is not mutated.

## What must never happen to this file

- It must never be replaced with real state from a real account. Terraform state
  routinely contains secrets in plaintext.
- `.gitignore` excludes `*.tfstate` everywhere except this directory, which is
  allowlisted deliberately. That allowlist exists for this fixture alone.
