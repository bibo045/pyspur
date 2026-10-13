# Design direction: Ledger

**Who it is for.** Freelancers and small agencies who invoice regularly and want to finish an invoice in under a minute. They are not accountants. They open the app to get paid, not to browse.

**Feel.** A calm, precise ledger. Warm paper neutrals, one ink-spruce accent used only for primary actions and focus, and status colour (paid, sent, overdue) as the only other colour. No gradients, no coloured KPI tiles, no decorative shapes.

## Tokens (`app/static/src/app.css`)

| Role | Token | Use |
| --- | --- | --- |
| Surface | `background` (warm paper), `surface` (white) | Page, cards, inputs |
| Text | `foreground` (ink), `muted-foreground` | Body, labels, meta |
| Lines | `border`, `border-strong` | Cards, controls |
| Action | `primary` (ink spruce), `ring` | Primary buttons, focus, active states |
| Status | `success`, `info`, `warning`, `destructive` | Paid, sent, attention, overdue and errors |

- **Type.** Instrument Sans for the interface. Tabular numerals for every amount. Instrument Serif only for page titles, the one display voice.
- **Shape.** 6px radius on controls, 10px on cards, a 1px hairline border and a faint shadow. Floating menus and dialogs use the only heavier shadow.
- **Motion.** Motion (vendored) for dialogs, menus, toasts, the drawer and step changes. Every animation is skipped when the user prefers reduced motion.

## Page structure

- **App shell.** Sidebar (Dashboard, Invoices, Clients, Services, Templates, Settings). A top bar with the business name. A mobile drawer below `lg`.
- **Dashboard.** Page title with the primary action (Create invoice). Four KPIs. Recent invoices table. A "Needs attention" list of overdue invoices.
- **Invoices.** Search, status filter chips, sortable table, row action menu (open, PDF, print, duplicate, delete draft, send), pagination, empty and no-match states.
- **Invoice editor.** Five steps: client, services, details, template, review and send. Desktop: form on the left, live PDF on the right. Mobile: single column with a fixed total bar. Finalize opens a confirmation dialog.
- **Invoice detail.** Amounts, line items, payment (record payment dialog), email (send or resend dialog), cancellation (reason required), history.
- **Clients and services.** Searchable lists, archive and restore through confirmation dialogs, dedicated form pages.
- **Templates.** Style gallery with a live sample PDF and brand controls.
- **Settings.** Tabs for business, invoicing, bank and payment, email, and demo data. Email connection status and a test send.

## Accessibility

- Visible focus ring on every control, including inside dialogs and menus.
- Dialogs use native `<dialog>` with focus moved in and Escape to close. Menus support arrow keys and Escape.
- Status is never conveyed by colour alone: every status is a label.
- Touch targets are at least 32px, and 36–44px for primary actions.
- Layout checked at 390px and 1440px with no horizontal page overflow.

## Stack notes

- Server-rendered Flask and Jinja, so the backend, PDFs and tests stay unchanged.
- Tailwind CSS v4 compiled to `app/static/css/app.css`. The semantic tokens follow shadcn/ui naming. No React, so shadcn's React components are not used.
- Build with `npm run build` after changing styles or templates' class names.
