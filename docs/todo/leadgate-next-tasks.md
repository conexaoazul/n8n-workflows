# Lead Gate Next Tasks

## Done on 2026-05-31

- Fixed Chatwoot duplicate phone handling in `chatwoot_lead_sync.py`.
- Added a Chatwoot API `User-Agent` to avoid Cloudflare `1010` blocks from Python `urllib`.
- Reused existing contacts by phone/email before trying to create new contacts.
- Updated existing contacts best-effort without blocking lead capture.
- Exposed Chatwoot sync `mode` in `/api/leads/automation`.
- Persisted commercial aliases in `lead_store`: `nome`, `empresa`, `segmento`, `interesse`, `desafio`, `ferramenta_atual`, `urgencia`, `orcamento`.
- Deployed `conexaoazul/n8n-workflows:leadgate-store-fix-20260531-1326`.
- Validated local, public, health and full lead POST.

## Rollback

Previous known image:

```bash
docker service update \
  --image conexaoazul/n8n-workflows:leadgate-chatwoot-fix-20260531-1323 \
  --update-order stop-first \
  n8n-workflows_app
```

Older stable rollback:

```bash
docker service update \
  --image conexaoazul/n8n-workflows:leadgate-auth-otp-20260531-0110 \
  --update-order stop-first \
  n8n-workflows_app
```

## Pending

- Add a direct internal Chatwoot endpoint or Cloudflare API allow rule for write calls if public `POST /contacts` gets blocked again.
- Keep WhatsApp OTP in fallback until an approved Authentication template exists. Current rejected template: `ca_codigo_verificacao_v2`.
- Sync leads from `/app/data/leads.jsonl` or `leads.db` into Odoo `consultas` with idempotency by email/phone.
- Publish CTA on the main Cloudflare Pages site pointing to `n8n-workflows.conexaoazul.com` with UTM and `conversion_flow`.
- Fix Prometheus memory alert PromQL to ignore containers without valid memory limits before changing service limits.
- Diagnose `c4associados_c4associados` field error `res_partner.ixc_has_active_contract` without updating modules in production.
