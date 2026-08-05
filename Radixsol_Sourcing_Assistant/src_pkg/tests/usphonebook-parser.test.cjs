const assert = require("node:assert/strict");
const test = require("node:test");

const parser = require("../frontend/usphonebook-parser.js");

test("builds a nationwide URL followed by state and city fallbacks", () => {
  assert.deepEqual(
    parser.buildProviderUrls("Alice Marie Chu", "Houston, TX"),
    [
      "https://www.usphonebook.com/alice-chu",
      "https://www.usphonebook.com/alice-chu/texas",
      "https://www.usphonebook.com/alice-chu/texas/houston",
    ],
  );
});

test("removes recruiting credentials and role text from provider searches", () => {
  assert.equal(parser.providerSearchName("Alicia Smith RN"), "alicia smith");
  assert.equal(
    parser.providerSearchName("ELIZABETH SHELTON MAYWEATHER REGISTERED NURSE"),
    "elizabeth mayweather",
  );
});

test("chooses the exact-name result in the requested city", () => {
  const response = parser.parseSnapshot({
    url: "https://www.usphonebook.com/alice-chu",
    title: "Alice Chu results",
    bodyText: "We've found 2 records",
    pageKind: "results",
    cards: [
      {
        name: "Alice Chu, Age 62",
        text: "Alice Chu, Age 62 Lives in San Diego, CA",
        href: "https://www.usphonebook.com/alice-chu/california-id",
      },
      {
        name: "Alice Chu, Age 59",
        text: "Alice Chu, Age 59 Lives in Houston, TX",
        href: "https://www.usphonebook.com/alice-chu/houston-id",
      },
    ],
  }, {
    expectedName: "Alice Chu",
    location: "Houston, TX",
  });
  assert.equal(response.action, "follow_profile");
  assert.equal(response.profileUrl, "https://www.usphonebook.com/alice-chu/houston-id");
});

test("returns exact-name profile URLs for second-stage ambiguous review", () => {
  const response = parser.parseSnapshot({
    url: "https://www.usphonebook.com/cevi-adams",
    title: "Cevi Adams results",
    bodyText: "We've found 2 records",
    pageKind: "results",
    cards: [
      {
        name: "Cevi Adams, Age 39",
        text: "Cevi Adams, Age 39 Lives in Visalia, CA",
        href: "https://www.usphonebook.com/find/person/cevi-adams/one",
      },
      {
        name: "Cevi Adams, Age 42",
        text: "Cevi Adams, Age 42 Lives in Visalia, CA",
        href: "https://www.usphonebook.com/find/person/cevi-adams/two",
      },
    ],
  }, {
    expectedName: "Cevi Adams",
    location: "Visalia, CA",
    jobTitle: "Registered Nurse",
  });
  assert.equal(response.action, "result");
  assert.equal(response.result.status, "multiple_matches");
  assert.equal(response.result.profiles.length, 2);
});

test("excludes exact-name profiles outside the requested city before review", () => {
  const response = parser.parseSnapshot({
    url: "https://www.usphonebook.com/cevi-adams",
    title: "Cevi Adams results",
    bodyText: "We've found 4 records",
    pageKind: "results",
    cards: [
      {
        name: "Cevi Adams",
        text: "Cevi Adams Lives in Visalia, CA",
        href: "https://www.usphonebook.com/find/person/cevi-adams/right-one",
      },
      {
        name: "Cevi Adams",
        text: "Cevi Adams Lives in Visalia, CA",
        href: "https://www.usphonebook.com/find/person/cevi-adams/right-two",
      },
      {
        name: "Cevi Adams",
        text: "Cevi Adams Lives in Fresno, CA",
        href: "https://www.usphonebook.com/find/person/cevi-adams/wrong-location",
      },
      {
        name: "Cevi Adams Johnson",
        text: "Cevi Adams Johnson Lives in Visalia, CA",
        href: "https://www.usphonebook.com/find/person/cevi-adams-johnson/wrong-name",
      },
    ],
  }, {
    expectedName: "Cevi Adams",
    location: "Visalia, CA",
    jobTitle: "Registered Nurse",
  });
  assert.equal(response.result.status, "multiple_matches");
  assert.deepEqual(
    response.result.profiles.map((profile) => profile.profile_url),
    [
      "https://www.usphonebook.com/find/person/cevi-adams/right-one",
      "https://www.usphonebook.com/find/person/cevi-adams/right-two",
    ],
  );
});

test("selects between exact name/location profiles using job title", () => {
  const decision = parser.selectReviewedProfile([
    {
      profile_url: "https://www.usphonebook.com/find/person/cevi-adams/right",
      matched_name: "Cevi Adams",
      phones: ["(559) 555-0187"],
      emails: [],
      current_addresses: ["100 Main St, Visalia, CA 93291"],
      previous_addresses: [],
      addresses: ["100 Main St, Visalia, CA 93291"],
      card_location_score: 1,
      work_history: [{
        title: "RN",
        organization: "Valley Hospital",
        location: "Visalia, CA",
        summary: "Registered Nurse at Valley Hospital in Visalia, CA",
      }],
      education: [],
    },
    {
      profile_url: "https://www.usphonebook.com/find/person/cevi-adams/wrong",
      matched_name: "Cevi Adams",
      phones: ["(310) 555-0120"],
      emails: [],
      current_addresses: ["200 Oak St, Visalia, CA 93291"],
      previous_addresses: [],
      addresses: ["200 Oak St, Visalia, CA 93291"],
      card_location_score: 1,
      work_history: [{
        title: "Office Manager",
        organization: "Example Co",
        location: "Visalia, CA",
        summary: "Office Manager at Example Co in Visalia, CA",
      }],
      education: [],
    },
  ], "Visalia, CA", "Registered Nurse (RN)");
  assert.equal(
    decision.selected.profile.profile_url,
    "https://www.usphonebook.com/find/person/cevi-adams/right",
  );
  const result = parser.resultFromReviewedProfile(decision.selected);
  assert.equal(result.status, "success");
  assert.deepEqual(result.phones, ["(559) 555-0187"]);
  assert.equal(result.match_evidence.reviewed_profiles, true);
});

test("keeps equally supported reviewed profiles ambiguous", () => {
  const profile = {
    matched_name: "Cevi Adams",
    phones: ["(559) 555-0187"],
    emails: [],
    current_addresses: ["100 Main St, Visalia, CA 93291"],
    previous_addresses: [],
    addresses: ["100 Main St, Visalia, CA 93291"],
    work_history: [{ summary: "Registered Nurse in Visalia, CA" }],
    education: [],
  };
  const decision = parser.selectReviewedProfile([
    { ...profile, profile_url: "https://www.usphonebook.com/find/person/cevi-adams/one" },
    { ...profile, profile_url: "https://www.usphonebook.com/find/person/cevi-adams/two" },
  ], "Visalia, CA", "Registered Nurse");
  assert.equal(decision.selected, null);
});

test("normalizes contacts only when name and current location match", () => {
  const response = parser.parseSnapshot({
    url: "https://www.usphonebook.com/alice-chu/houston-id",
    title: "Alice Chu",
    bodyText: "Alice Chu Houston TX",
    pageKind: "profile",
    profileName: "Alice Marie Chu",
    phones: ["+1 713-555-0187", "(713) 555-0187"],
    emails: ["mailto:Alice@example.test", "Send email"],
    currentAddresses: ["100 Main St, Houston, TX 77002"],
    previousAddresses: ["200 First Ave, Austin, TX 78701"],
  }, {
    expectedName: "Alice Chu",
    location: "Houston, TX",
  });
  assert.equal(response.result.status, "success");
  assert.deepEqual(response.result.phones, ["(713) 555-0187"]);
  assert.deepEqual(response.result.emails, ["alice@example.test"]);
  assert.ok(response.result.confidence >= 0.9);
});

test("withholds contacts when current location does not match", () => {
  const result = parser.parseProfile({
    url: "https://www.usphonebook.com/alice-chu/example",
    title: "Alice Chu",
    bodyText: "Alice Chu San Diego CA",
    profileName: "Alice Chu",
    phones: ["713-555-0187"],
    emails: ["alice@example.test"],
    currentAddresses: ["100 Main St, San Diego, CA 92101"],
    previousAddresses: [],
  }, "Alice Chu", "Houston, TX");
  assert.equal(result.status, "location_unverified");
  assert.deepEqual(result.phones, []);
  assert.deepEqual(result.emails, []);
});

test("reports a browser challenge without attempting to bypass it", () => {
  const result = parser.parseProfile({
    url: "https://www.usphonebook.com/alice-chu",
    title: "Just a moment...",
    bodyText: "Performing security verification",
    hasCaptcha: true,
  }, "Alice Chu", "Houston, TX");
  assert.equal(result.status, "captcha_required");
  assert.deepEqual(result.phones, []);
});
