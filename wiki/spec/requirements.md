# Requirements

[← Wiki home](../README.md) · Next: [architecture](architecture.md) · [decisions](decisions.md)

## 1. Original requirements (verbatim, RU)

> **1. Цель и принципы проекта**
>
> Цель: Создать локальную утилиту для автоматического мониторинга, отслеживания трендов и анализа репозиториев GitHub в заданных технологических доменах.
>
> Целевая платформа: macOS (оптимизация под запуск локальных моделей на Apple Silicon через MLX/Ollama, хранение данных в локальных файлах).
>
> Философия алертинга: Никаких спам-алертов в реальном времени. Только пакетные регулярные дайджесты.
>
> **2. Поэтапный план реализации**
>
> **Часть 1: CLI-ядро и бэкенд-пайплайн (Без дашборда)** — автономный скрипт/демон, который собирает данные, фильтрует их и шлет отчеты.
> - Интервьюирование и генерация запросов (Domain Profile Skill): скрипт (или интерактивный промпт) формирует поисковую матрицу для GitHub на основе детального описания области.
> - Сбор данных с GitHub (Collector Engine): GitHub Search API с пагинацией и секционированием по датам/языкам для обхода ограничений. Сбор метрик: звезды, форки, контрибьюторы, регулярность коммитов, новые релизы и измененные ключевые файлы (pyproject.toml, package.json).
> - Триаж и классификация (Deciders-модели): вместо общих LLM — легковесные Deciders-модели (jev-like, clef и т.п.) для бинарного триажа и быстрой классификации: «Релевантно ли это нашему домену? Есть ли здесь готовый инструмент/протокол или это мусор/пустышка?».
> - Хранение (Local Data Store): локальная база SQLite + векторное расширение (sqlite-vss или DuckDB) для истории метрик и семантических эмбеддингов.
> - Доставка отчетов (Telegram): структурированные пакетные дайджесты (утром/вечером) в Telegram-чат.
>
> **Часть 2: Дашборд и визуализация ландшафта** — после отладки стабильной работы первой части.
> - Интерфейс управления доменами: локальное веб-приложение (Streamlit или FastAPI) для добавления, удаления и изменения периодичности сканирования областей.
> - Интерактивная карта ландшафта: UMAP/t-SNE + кластеризация по эмбеддингам; где группируются основные игроки, где нишевые форки.
> - Аналитические графики: динамика роста звезд (stars_delta), индексы активности (Velocity), изменения технологического стека по проектам.
>
> Note: for triage by local models and Telegram access there is an example: `as.tegerun.cli`. Use similar approaches where they fit.

## 2. Requirement IDs (traceability)

Every task in the plan cites the IDs it implements. Coverage matrix: [ROADMAP § coverage](../plans/ROADMAP.md#requirement-coverage).

| ID | Requirement (EN) | Interpretation / acceptance |
|---|---|---|
| **R-G1** | Local utility for monitoring/trend tracking/analysis of GitHub repos in configured technology domains | Single Python CLI `repka`, all data in the project folder. |
| **R-G2** | macOS, local models on Apple Silicon via MLX/Ollama, local files | Deciders run on MPS/MLX; embeddings via Ollama; SQLite file. Linux not a goal. |
| **R-G3** | No real-time alerts, only batched regular digests | Nothing is sent outside digest slots; empty digests are not sent. |
| **R-1.1** | Domain Profile: interview → GitHub search matrix from a detailed domain description | `repka domain new` (interactive wizard) + Claude Code skill `domain-profile`; output `domains/<id>.yaml`; `repka domain plan <id>` shows the expanded query matrix. |
| **R-1.2a** | Collector: Search API with pagination and date/language partitioning to get around limits | Query matrix × date bisection, ≤1000 results per slice, resumable. |
| **R-1.2b** | Metrics: stars, forks, contributors, commit regularity, new releases, changed key files | Snapshots per scan; events for new releases and key-file changes; dependency stack parsed. |
| **R-1.3** | Triage with lightweight decider models (binary + quick classification) | `relevant` (noul), `substance` (score), `kind` (choice) via local deciders; thresholds. |
| **R-1.4** | SQLite + vector extension for metric history and embeddings | SQLite + sqlite-vec (fallback numpy); history tables; embeddings tagged with model. |
| **R-1.5** | Telegram batched digests morning/evening | User-account sender (fast-mcp-telegram), slots, coalescing, dedupe of reported items. |
| **R-1.6** | Autonomous script/daemon | `repka run` one pass; launchd agent hourly; lock; preflight/doctor. |
| **R-2.1** | Local web UI to add/remove domains and change scan periodicity | Streamlit app page "Domains". |
| **R-2.2** | Landscape map: UMAP/t-SNE + clustering over embeddings | Streamlit page with interactive scatter, clusters, forks highlighted. |
| **R-2.3** | Charts: stars_delta, Velocity, tech stack changes | Streamlit page with time series and stack diff. |

## 3. Clarifications and assumptions (made by the planner, revisit if wrong)

| # | Assumption | Why |
|---|---|---|
| A1 | Language: Python ≥ 3.12, managed by `uv`; CLI name `repka`, package `repka`. | Matches reference project; ML ecosystem. |
| A2 | "jev-like, clef" = local decision models with noul/choice/score answers. Default model is LiquidAI d1-3B (proven locally); Clef via MLX is optional after a spike; hosted Jev is an off-by-default remote backend (maybe never built). | Requirement says local; Jev weights are not public. |
| A3 | sqlite-vss is replaced by sqlite-vec (successor by the same author); DuckDB not used. | vss unmaintained. |
| A4 | Telegram delivery uses the user's own account (Telegram app `api_id`/`api_hash`, fast-mcp-telegram), default chat `"me"`. | User's choice (2026-11-07); same path as the reference project. |
| A5 | One digest covers all domains, grouped by domain; per-domain chat routing is a later option. | Simplest useful default. |
| A6 | GitHub token (fine-grained PAT, public read) is available. | Search API needs auth for 30 req/min. |
| A7 | Scale: up to ~10 domains × a few thousand repos each. | Drives the choice of brute-force fallback and simple scheduling. |
| A8 | Phase 2 is planned at task level only; it is refined after Phase 1 runs stably. | Requirements say Phase 2 follows Phase 1 stabilization. |
