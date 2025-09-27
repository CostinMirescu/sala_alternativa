# Handoff – Sala Alternativă (pilot)

## Ce merge acum
- Monitor (auto/off/active/end), QR regenerat în fereastră.
- Check-in/out cu token semnat, device_id, rate-limit.
- Rapoarte diriginți (HTML + export ZIP).
- Auto-sesiuni oprite/pornite prin env (`AUTO_SESSIONS_ENABLED`).

## Ce e în lucru / probleme
- Monitor cu `?session_id=`: comportament intermitent – de diagnosticat.
- DB local trebuie să fie `instance/sala.db` (nu `C:\instance\sala.db`).
- Render folosește `/data/sala.db`.

## Mediu/ENV
- Local: `.env` → `DATABASE_PATH=instance/sala.db`, `TZ=Europe/Bucharest`.
- Render: `DATABASE_URL=sqlite:////data/sala.db`, `TZ=Europe/Bucharest`, `FORCE_PROXY_FIX=true`, `PREFERRED_URL_SCHEME=https`.

## Comenzi utile (local)
