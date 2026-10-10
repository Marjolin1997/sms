import { describe, expect, it } from "vitest";
import { parseContacts } from "../pages/Contacts.jsx";

describe("parseContacts()", () => {
  it("reads a CSV with a header in any column order", () => {
    const r = parseContacts("email,first_name,phone\nana@example.com,Ana,+355691234567\n");
    expect(r.rows).toEqual([{ email: "ana@example.com", first_name: "Ana", phone: "+355691234567" }]);
    expect(r.skipped).toBe(0);
  });
  it("accepts semicolons, tabs, quotes and 'name'/'mobile' aliases", () => {
    const r = parseContacts('mobile;name\n"0355691234567";"Besa"');
    expect(r.rows[0]).toMatchObject({ first_name: "Besa" });
    expect(r.rows[0].phone).toMatch(/^\+/);
  });
  it("without a header, treats an @ cell as email and the rest positionally", () => {
    const r = parseContacts("+355691234567,ana@example.com,Ana,Hoxha");
    expect(r.rows[0]).toEqual({ phone: "+355691234567", email: "ana@example.com", first_name: "Ana", last_name: "Hoxha" });
  });
  it("skips rows without phone or email and counts them", () => {
    const r = parseContacts("phone,email\n,\n+355691234567,\n\nfoo,");
    expect(r.rows).toHaveLength(2);
    expect(r.skipped).toBe(1);
  });
  it("returns nothing for empty input", () => {
    expect(parseContacts("  \n\n")).toEqual({ rows: [], skipped: 0 });
  });
});
