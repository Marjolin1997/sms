// Fjalori shqip: çelësi = teksti anglisht në kod. Ndahet sipas faqeve; `npm run i18n:check`
// verifikon që çdo tekst i përdorur ka përkthim dhe që s'ka çelësa të papërdorur.
import common from "./common.js";
import dashboard from "./dashboard.js";
import messaging from "./messaging.js";
import campaigns from "./campaigns.js";
import contacts from "./contacts.js";
import setup from "./setup.js";
import money from "./money.js";
import staff from "./staff.js";
import security from "./security.js";
import reports from "./reports.js";

export default { ...common, ...dashboard, ...messaging, ...campaigns, ...contacts, ...setup, ...money, ...staff, ...security, ...reports };
