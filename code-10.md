# Mercados

Bot de estados de bolsas mundiales — Notificador del estado de bolsas mundiales.

## Resumen

Supervisa cinco bolsas mediante el protocolo de estates por mercado:

| MIC  | Bolsa              | País      | Moneda | Calendario          |
|------|--------------------|-----------|--------|---------------------|
| XMAD | Bolsa de Madrid    | España    | EUR    | exchange_calendars  |
| BVMF | B3                 | Brasil    | BRL    | exchange_calendars  |
| BMEX | BMV                | México    | MXN    | exchange_calendars  |
| XSPX | SP Global BMI      | Perú      | PEN    | exchange_calendars  |
| XFJI | South Pacific Stock Exchange | Fiyi | FJD | synthetic (Lun–Jue 08:30–13:00) |

## Configuración

1. Copiar `config/config.example.yaml` a `config/config.yaml`
2. Configurar credenciales de backend (webhook de Discord / token de bot de Slack / bot de Telegram)
3. Ajustar `language` a la configuración regional deseada

## Ejecutar

```bash
pip install -r requirements.txt
python -m src.main
