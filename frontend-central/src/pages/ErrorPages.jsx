import { t } from "../i18n.jsx";

const Page = ({ code, title, children }) => (
  <div className="errorstate" role="alert" data-error-code={code}>
    <div className="empty-i" aria-hidden>{code}</div>
    <b>{title}</b>
    <div className="muted">{children}</div>
    <a className="btn" href="#dashboard">{t("Back to overview")}</a>
  </div>
);
export const Forbidden = () => <Page code="403" title={t("You don't have access to this page")}>{t("Your role doesn't include it.")}</Page>;
export const NotFound = () => <Page code="404" title={t("This page doesn't exist")}>{t("The link may be old or mistyped. Use the menu to find what you need.")}</Page>;
