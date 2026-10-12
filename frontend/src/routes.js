// Tabela e vetme e rrugëve të konsolës ENTERPRISE (klient + staf i platformës). Të dhëna të pastra (pa React):
// menyja, kontrolli i lejeve, breadcrumb-et dhe zgjidhja e hash-it dalin nga këtu, ndaj testohen pa DOM.
// Rregull: UI-ja fsheh/çaktivizon sipas lejeve që njeh, por AUTORITETI është serveri (çdo veprim rikontrollohet atje).
// Admini i Central NUK hyn këtu: ka aplikacion dhe auth të veçantë (shih docs/M11_FRONTEND.md).
import { T } from "./i18n.jsx";

export const DEFAULT_ROUTE = "dashboard";
export const STAFF_GROUP = "Staff";

// perm: string ose "a|b" (mjafton një); global: faqe pa llogari të zgjedhur (staf)
// detail: faqja ka nën-rrugë detaji (#senders/12) që hapet edhe pa llogari të zgjedhur nga stafi (serveri e gjen pronarin)
// load: import dinamik (code splitting); faqja ngarkohet vetëm kur hapet.
export const NAV = [
  { group: "", items: [{ id: "dashboard", label: T("Overview"), icon: "◧", perm: null, load: () => import("./pages/Dashboard.jsx"), desc: T("Where things stand right now.") }] },
  { group: T("Messaging"), items: [
    { id: "send", label: T("Send"), icon: "➤", perm: "messages:send", load: () => import("./pages/Send.jsx"), desc: T("Send an SMS or an email. You see the price before you send.") },
    { id: "messages", label: T("Message history"), icon: "☷", perm: "messages:read", load: () => import("./pages/Messages.jsx"), desc: T("Everything you've sent and what happened to it.") },
    { id: "inbox", label: T("Inbox"), icon: "↩", perm: "inbox:read", load: () => import("./pages/Inbox.jsx"), desc: T("Replies from your recipients, and automatic answers to keywords.") },
    { id: "reports", label: T("Reports"), icon: "▤", perm: "reports:read", load: () => import("./pages/Reports.jsx"), desc: T("Usage, costs and CSV exports.") },
    { id: "campaigns", label: T("Campaigns"), icon: "✉", perm: "campaigns:read", load: () => import("./pages/Campaigns.jsx"), desc: T("Send to a whole list on a schedule, with a budget cap.") },
  ] },
  { group: T("Audience"), items: [
    { id: "contacts", label: T("Contacts"), icon: "☰", perm: "contacts:read", load: () => import("./pages/Contacts.jsx"), desc: T("Your people, their lists and what they agreed to receive.") },
  ] },
  { group: T("Setup"), items: [
    { id: "senders", label: T("Sender IDs & templates"), icon: "✦", perm: "sender:request", detail: true, load: () => import("./pages/Senders.jsx"), desc: T("The name people see, and reusable message templates. Both are reviewed by us.") },
    { id: "email", label: T("Email domains"), icon: "@", perm: "email:read", load: () => import("./pages/EmailDomains.jsx"), desc: T("Prove you own your domain so mail lands in the inbox.") },
    { id: "webhooks", label: T("Webhooks & events"), icon: "↯", perm: "events:read", load: () => import("./pages/Webhooks.jsx"), desc: T("Get delivery updates pushed to your own system.") },
  ] },
  { group: T("Money"), items: [
    { id: "wallet", label: T("Wallet"), icon: "◎", perm: "wallet:read", load: () => import("./pages/Wallet.jsx"), desc: T("Prepaid balance for SMS. Every movement is on the ledger.") },
    { id: "billing", label: T("Billing"), icon: "€", perm: "billing:read", load: () => import("./pages/Billing.jsx"), desc: T("Your plan, invoices and payments.") },
  ] },
  { group: T("Developers"), items: [
    { id: "developers", label: T("Developer guide"), icon: "‹›", perm: "keys:self", load: () => import("./pages/Developers.jsx"), global: true, desc: T("Copy-paste examples, webhook verification and the API reference.") },
    { id: "keys", label: T("API keys"), icon: "⚿", perm: "keys:self|keys:manage", load: () => import("./pages/Keys.jsx"), global: true, desc: T("Keys let your software talk to the platform.") },
  ] },
  { group: T("Staff"), items: [
    { id: "approvals", label: T("Approvals"), icon: "✓", perm: "sender:review|template:review", load: () => import("./pages/Approvals.jsx"), global: true, desc: T("Sender IDs and templates waiting for a decision.") },
    { id: "accounts", label: T("Accounts"), icon: "☖", perm: "monitor:read", load: () => import("./pages/Accounts.jsx"), global: true, desc: T("Every customer account at a glance.") },
    { id: "rates", label: T("Rates & routes"), icon: "%", perm: "rates:read", load: () => import("./pages/Rates.jsx"), global: true, desc: T("Price lists, effective dates and which provider carries which country.") },
    { id: "finance", label: T("Finance"), icon: "⊕", perm: "topup:confirm", load: () => import("./pages/Finance.jsx"), global: true, desc: T("Confirm top-ups and make audited balance corrections.") },
    { id: "providers", label: T("Provider health"), icon: "♥", perm: "monitor:read", load: () => import("./pages/Providers.jsx"), global: true, desc: T("Delivery rate, errors and stuck messages for each SMS provider.") },
    { id: "security", label: T("Security"), icon: "⛨", perm: "monitor:read", load: () => import("./pages/Security.jsx"), global: true, desc: T("Two-factor authentication for your staff key.") },
    { id: "admin", label: T("Admin"), icon: "⚙", perm: "keys:manage", load: () => import("./pages/Admin.jsx"), global: true, desc: T("Kill switches and the audit log.") },
  ] },
];

export const isStaffPrincipal = (me) => !me.owner_ref;

/** Leja e njohur nga /v1/me. `perm` bosh ⇒ e hapur; "*" ⇒ gjithçka; "a|b" ⇒ mjafton një. Vetëm për UI. */
export const can = (me, perm) =>
  !perm || (me.permissions || []).includes("*") || perm.split("|").some((p) => (me.permissions || []).includes(p));

const ALL = NAV.flatMap((g) => g.items.map((i) => ({ ...i, group: g.group })));
export const findRoute = (id) => ALL.find((r) => r.id === id) || null;

/** Grupet e menysë për këtë përdorues. Klienti nuk sheh kurrë grupin Staff; stafi e ka pas Overview. */
export function visibleGroups(me) {
  const staff = isStaffPrincipal(me);
  let groups = NAV.map((g) => ({ ...g, items: g.items.filter((i) => can(me, i.perm) && (staff || g.group !== STAFF_GROUP)) })).filter((g) => g.items.length);
  if (staff) groups = [groups[0], ...groups.filter((g) => g.group === STAFF_GROUP), ...groups.slice(1).filter((g) => g.group !== STAFF_GROUP)].filter(Boolean);
  return groups;
}

/** "#senders/12?x=1" → { id: "senders", rest: ["12"] }. Çdo gjë jashtë [a-z0-9-] është e pavlefshme (deep-link i sigurt). */
export function parseHash(hash) {
  const raw = String(hash || "").replace(/^#/, "").split("?")[0];
  const [id, ...rest] = raw.split("/");
  if (!id) return { id: DEFAULT_ROUTE, rest: [] };
  return /^[a-z0-9-]{1,40}$/.test(id) ? { id, rest: rest.filter(Boolean) } : { id: null, rest: [] };
}

/**
 * Zgjidhja e vetme e rrugës: { status: "ok" | "forbidden" | "not_found", route?, groups, crumbs }.
 * Rruga e panjohur ⇒ 404; rruga ekziston por përdoruesi s'e ka lejen (ose klienti te Staff) ⇒ 403. Kurrë riudhëzim i heshtur.
 */
export function resolveRoute(me, hash) {
  const groups = visibleGroups(me);
  const { id, rest } = parseHash(hash);
  const route = id && findRoute(id);
  if (!route) return { status: "not_found", groups, id, crumbs: [] };
  const visible = groups.flatMap((g) => g.items).find((i) => i.id === route.id);
  if (!visible) return { status: "forbidden", groups, id, route, crumbs: [] };
  const crumbs = [...(route.group ? [{ label: route.group }] : []), { label: route.label, href: `#${route.id}` }];
  return { status: "ok", groups, id, rest, route: visible, crumbs };
}
