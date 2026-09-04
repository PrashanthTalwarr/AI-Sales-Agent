"""
HUBSPOT INTEGRATION — Pushes scored leads to HubSpot CRM.

Structure:
  Company — one per protocol (Rocket Pool, Pendle, etc.)
  Contact — one per person emailed, linked to their company

Dedup:
  Companies — search by name before creating; skip if exists
  Contacts  — search by firstname+lastname before creating; skip if exists,
               log "already sent mail to this person"
"""

import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from hubspot import HubSpot
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate
    from hubspot.crm.contacts import SimplePublicObjectInput as ContactUpdateInput
    from hubspot.crm.companies import SimplePublicObjectInputForCreate as CompanyInput
    from hubspot.crm.properties import PropertyCreate
    HAS_HUBSPOT = True
except ImportError:
    HAS_HUBSPOT = False


DISCOVERY_CONTACT_PROPERTIES = [
    ("discovery_protocol_name",    "Discovery: Protocol Name",      "string", "text"),
    ("discovery_tvl",              "Discovery: TVL (USD)",           "number", "number"),
    ("discovery_category",         "Discovery: Category",            "string", "text"),
    ("discovery_composite_score",  "Discovery: Composite Score",     "number", "number"),
    ("discovery_score_tier",       "Discovery: Score Tier",          "string", "text"),
    ("discovery_outreach_status",  "Discovery: Outreach Status",     "string", "text"),
    ("discovery_preferred_channel","Discovery: Preferred Channel",   "string", "text"),
    ("discovery_twitter",          "Discovery: Twitter Handle",      "string", "text"),
    ("discovery_github",           "Discovery: GitHub Username",     "string", "text"),
]

DISCOVERY_COMPANY_PROPERTIES = [
    ("discovery_tvl",              "Discovery: TVL (USD)",           "number", "number"),
    ("discovery_category",         "Discovery: Category",            "string", "text"),
    ("discovery_score_tier",       "Discovery: Score Tier",          "string", "text"),
    ("discovery_composite_score",  "Discovery: Composite Score",     "number", "number"),
    ("discovery_audit_status",     "Discovery: Audit Status",        "string", "text"),
    ("discovery_bounty_platform",  "Discovery: Bounty Platform",     "string", "text"),
    ("discovery_shipping_velocity","Discovery: Shipping Velocity",   "string", "text"),
    ("discovery_chain",            "Discovery: Chain(s)",            "string", "text"),
    ("discovery_ai_signals",       "Discovery: AI Tool Signals",     "string", "text"),
]


def get_hubspot_client():
    if not HAS_HUBSPOT:
        return None
    api_key = os.getenv("HUBSPOT_API_KEY", "")
    if not api_key:
        return None
    return HubSpot(access_token=api_key)


def ensure_custom_properties(client) -> bool:
    """Create discovery_* properties on contacts and companies if missing."""
    try:
        for obj_type, props in [("contacts", DISCOVERY_CONTACT_PROPERTIES), ("companies", DISCOVERY_COMPANY_PROPERTIES)]:
            existing = client.crm.properties.core_api.get_all(object_type=obj_type)
            existing_names = {p.name for p in existing.results}
            for name, label, prop_type, field_type in props:
                if name not in existing_names:
                    client.crm.properties.core_api.create(
                        object_type=obj_type,
                        property_create=PropertyCreate(
                            name=name, label=label,
                            type=prop_type, field_type=field_type,
                            group_name="contactinformation" if obj_type == "contacts" else "companyinformation",
                        )
                    )
                    logger.info("HubSpot property created: %s on %s", name, obj_type)
        print("  HubSpot: custom properties ready", flush=True)
        return True
    except Exception as e:
        logger.error("HubSpot property setup failed: %s", e)
        return False


def find_company(client, protocol_name: str) -> Optional[str]:
    """Search HubSpot for a company by name. Returns company id or None."""
    try:
        from hubspot.crm.companies import PublicObjectSearchRequest
        results = client.crm.companies.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[{"filters": [{"propertyName": "name", "operator": "EQ", "value": protocol_name}]}],
                properties=["name"],
                limit=1,
            )
        )
        if results.total > 0:
            return results.results[0].id
    except Exception as e:
        logger.warning("HubSpot company search failed for %s: %s", protocol_name, e)
    return None


def create_company(client, protocol_name: str, enrichment: dict, score_data: dict) -> Optional[str]:
    """Find or create a HubSpot company for a protocol. Returns company id."""
    if not protocol_name or not protocol_name.strip():
        logger.warning("create_company: skipping — empty protocol name")
        return None
    existing_id = find_company(client, protocol_name)
    if existing_id:
        logger.info("HubSpot company already exists: %s (id=%s)", protocol_name, existing_id)
        print(f"  ~ Company exists: {protocol_name}", flush=True)
        return existing_id

    try:
        props = {
            "name":                    protocol_name,
            "discovery_tvl":             str(int(enrichment.get("tvl_usd", 0) or 0)),
            "discovery_category":        enrichment.get("category", ""),
            "discovery_score_tier":      score_data.get("tier", ""),
            "discovery_composite_score": str(score_data.get("composite", 0)),
            "discovery_audit_status":    "audited" if enrichment.get("has_been_audited") else "not_audited",
            "discovery_bounty_platform": enrichment.get("bounty_platform", "none"),
            "discovery_shipping_velocity": enrichment.get("shipping_velocity", ""),
            "discovery_chain":           ", ".join(enrichment.get("chains_deployed", []) or []),
            "discovery_ai_signals":      ", ".join(enrichment.get("ai_tool_signals", []) or []),
        }
        response = client.crm.companies.basic_api.create(
            simple_public_object_input_for_create=CompanyInput(properties=props)
        )
        logger.info("HubSpot company created: %s (id=%s)", protocol_name, response.id)
        print(f"  + Company created: {protocol_name}", flush=True)
        return response.id
    except Exception as e:
        logger.error("HubSpot company create failed for %s: %s", protocol_name, e)
        return None


def find_contact(client, firstname: str, lastname: str) -> Optional[str]:
    """Search HubSpot for a contact by firstname + lastname. Returns contact id or None."""
    try:
        from hubspot.crm.contacts import PublicObjectSearchRequest
        results = client.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[{"filters": [
                    {"propertyName": "firstname", "operator": "EQ", "value": firstname},
                    {"propertyName": "lastname",  "operator": "EQ", "value": lastname},
                ]}],
                properties=["firstname", "lastname"],
                limit=1,
            )
        )
        if results.total > 0:
            return results.results[0].id
    except Exception as e:
        logger.warning("HubSpot contact search failed for %s %s: %s", firstname, lastname, e)
    return None


def create_contact(
    client,
    protocol_name: str,
    person: dict,
    company_id: str,
    enrichment: dict,
    score_data: dict,
) -> Optional[str]:
    """
    Find or create a HubSpot contact for a person. Associates with company.
    Logs clearly if contact already exists (mail already sent to this person).
    Returns contact id or None.
    """
    full_name = (person.get("persona") or "").strip() or protocol_name
    if not full_name.strip():
        logger.warning("create_contact: skipping — no persona name for %s", protocol_name)
        return None
    name_parts = full_name.split(" ", 1)
    firstname = name_parts[0]
    lastname  = name_parts[1] if len(name_parts) > 1 else f"({protocol_name})"

    existing_id = find_contact(client, firstname, lastname)
    if existing_id:
        logger.info(
            "HubSpot contact already exists: %s %s @ %s — mail already sent to this person",
            firstname, lastname, protocol_name
        )
        print(f"  ~ Already sent mail to {full_name} @ {protocol_name} — skipping", flush=True)
        # Still associate with company and update preferred channel
        if company_id:
            try:
                client.crm.contacts.associations_api.create_default(
                    contact_id=existing_id,
                    to_object_type="companies",
                    to_object_id=company_id,
                )
                logger.debug("Associated existing contact %s with company %s", existing_id, company_id)
            except Exception as e:
                logger.debug("Association skipped for existing contact %s: %s", existing_id, e)
        try:
            client.crm.contacts.basic_api.update(
                contact_id=existing_id,
                simple_public_object_input=ContactUpdateInput(properties={
                    "discovery_preferred_channel": person.get("channel", "") or "email",
                    "hs_lead_status": "ATTEMPTED_TO_CONTACT",
                })
            )
        except Exception as e:
            logger.debug("Property update skipped for existing contact %s: %s", existing_id, e)
        return existing_id

    try:
        props = {
            "firstname":                firstname,
            "lastname":                 lastname,
            "jobtitle":                 person.get("role", ""),
            "company":                  protocol_name,
            "hs_lead_status":           "ATTEMPTED_TO_CONTACT",
            "discovery_protocol_name":    protocol_name,
            "discovery_tvl":              str(int(enrichment.get("tvl_usd", 0) or 0)),
            "discovery_category":         enrichment.get("category", ""),
            "discovery_composite_score":  str(score_data.get("composite", 0)),
            "discovery_score_tier":       score_data.get("tier", ""),
            "discovery_outreach_status":  "sent",
            "discovery_preferred_channel": person.get("channel", "") or "email",
            "discovery_twitter":          person.get("twitter", ""),
            "discovery_github":           person.get("github", ""),
        }
        # Use real contact email; if none, generate a unique placeholder to avoid HubSpot dedup collisions
        import uuid
        test_email = os.getenv("RESEND_TEST_EMAIL", "")
        email = person.get("real_email", "")
        if not email or email == test_email or "resend.dev" in email:
            email = f"{firstname.lower()}.{lastname.lower().replace(' ', '').replace('(', '').replace(')', '')}.{uuid.uuid4().hex[:6]}@discovery-placeholder.example"
        props["email"] = email

        response = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        contact_id = response.id
        logger.info("HubSpot contact created: %s (id=%s)", full_name, contact_id)
        print(f"  + Contact created: {full_name} @ {protocol_name}", flush=True)

        # Associate contact with company
        if company_id:
            try:
                client.crm.associations.v4.basic_api.create_default(
                    from_object_type="contacts",
                    from_object_id=contact_id,
                    to_object_type="companies",
                    to_object_id=company_id,
                )
                logger.debug("Associated contact %s with company %s", contact_id, company_id)
            except Exception as e:
                logger.warning("Failed to associate contact %s with company %s: %s", contact_id, company_id, e)

        return contact_id

    except Exception as e:
        err_str = str(e)
        if "409" in err_str or "CONTACT_EXISTS" in err_str or "already exists" in err_str.lower():
            logger.warning("HubSpot contact already exists: %s @ %s", full_name, protocol_name)
            print(f"  ~ Already sent mail to {full_name} @ {protocol_name} — skipping", flush=True)
        else:
            logger.error("HubSpot contact create failed for %s @ %s: %s", full_name, protocol_name, err_str[:200])
            print(f"  ! Failed to create contact {full_name} @ {protocol_name}", flush=True)
        return None


def push_batch_to_hubspot(
    scored_leads: list,
    enrichment_map: dict,
    persona_map: dict,
    send_results: dict = None,
) -> dict:
    """
    For each warm/hot protocol where mail was sent:
      1. Find or create a Company record
      2. Find or create a Contact per person emailed, linked to that company
    """
    print("\n" + "=" * 60, flush=True)
    print("HUBSPOT — Pushing leads and contacts to CRM", flush=True)
    print("=" * 60 + "\n", flush=True)

    client = get_hubspot_client()

    qualified_protocols = {
        lead.protocol_name
        for lead in scored_leads
        if lead.score_tier in ("hot", "warm")
    }

    # Group emailed people by protocol
    people_by_protocol: dict[str, list] = {}
    if send_results:
        for r in send_results.get("results", []):
            proto = r.get("protocol", "").strip()
            if not proto:
                continue
            if r.get("status") in ("sent", "failed") and proto in qualified_protocols:
                people_by_protocol.setdefault(proto, []).append(r)
    else:
        for lead in scored_leads:
            if lead.score_tier in ("hot", "warm") and lead.protocol_name.strip():
                persona = persona_map.get(lead.protocol_name, {})
                people_by_protocol[lead.protocol_name] = [{
                    "protocol": lead.protocol_name,
                    "persona": persona.get("name", lead.protocol_name),
                    "role": persona.get("role", ""),
                    "to": persona.get("email", ""),
                }]

    # Log exactly what's about to be pushed — helps trace phantom entries
    logger.info("HubSpot push: %d protocols → %s", len(people_by_protocol), list(people_by_protocol.keys()))
    print(f"  HubSpot: pushing {len(people_by_protocol)} protocol(s): {list(people_by_protocol.keys())}", flush=True)

    if not client:
        print("  HubSpot not configured — dry run\n", flush=True)
        for proto, people in people_by_protocol.items():
            print(f"  [dry-run] Company: {proto}", flush=True)
            for p in people:
                print(f"    [dry-run] Contact: {p.get('persona')} ({p.get('role')})", flush=True)
        return {}

    ensure_custom_properties(client)

    results = {}
    companies_created = contacts_created = contacts_skipped = 0

    for protocol_name, people in people_by_protocol.items():
        enrichment = enrichment_map.get(protocol_name, {})
        lead = next((l for l in scored_leads if l.protocol_name == protocol_name), None)
        score_data = {
            "composite": lead.composite_score if lead else 0,
            "tier": lead.score_tier if lead else "",
        }

        print(f"\n  [{protocol_name}]", flush=True)

        # Step 1: find or create the company
        company_id = create_company(client, protocol_name, enrichment, score_data)
        if company_id:
            companies_created += 1
            results[protocol_name] = {"company_id": company_id, "contacts": []}

        # Step 2: find or create each contact, link to company
        for person in people:
            contact_id = create_contact(client, protocol_name, person, company_id, enrichment, score_data)
            if contact_id:
                if results.get(protocol_name):
                    results[protocol_name]["contacts"].append(contact_id)
                contacts_created += 1
            else:
                contacts_skipped += 1

    print(
        f"\n  Companies: {companies_created} | Contacts created: {contacts_created} | Already existed: {contacts_skipped}",
        flush=True
    )
    return results
