"""
Deep board sweep for PROVEN-good companies. Unlike source_multi.py (narrow title
keywords), this dumps EVERY remote-US-eligible role from a target board and only
filters out obvious non-fits (core eng/research/technician/finance/HR/legal/
recruiting/design). Goal: catch adjacent commercial/ops/CS/SE/PM/analyst roles a
title-keyword filter misses. Read-only; prints JSON.

Usage: python3 scripts/source_deep.py --since 30
"""
import argparse, html, json, re, ssl, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
try:
    import certifi; _SSL=ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL=ssl.create_default_context()
from alice.pipeline import ats_client  # shared Greenhouse/Ashby/Lever fetch layer
UA="job-search-sourcer/1.2 (+personal use)"

# Validated good-fit boards across all sourcing rounds (confirmed-resolving slugs).
BOARDS=[
 # Industrial / IoT / manufacturing / field-service
    ("Fleetline","greenhouse","fleetline"),
    ("Northwind Systems","greenhouse","northwind"),
    ("Flowstate","greenhouse","flowstate"),
    ("Fictiv","greenhouse","fictiv"),
    ("Vertex Manufacturing","greenhouse","vertexmfg"),
    ("ServiceTrade","greenhouse","servicetrade"),
    ("BuildOps","greenhouse","buildops"),
    ("Fieldwire","greenhouse","fieldwire"),
    ("ShipBob","greenhouse","shipbobinc"),
    ("Span","ashby","span"),
    ("Trailhead Robotics","greenhouse","trailheadrobotics"),
    ("Boreal CAD","workday","boreal/wd1/Boreal"),   # CAD/PLM (FlowCAD) — on-domain; Workday CXS sweep
 # AI-native infra / agents / data
    ("Lumen Search","greenhouse","lumenwork"),
    ("Arize AI","greenhouse","arizeai"),
    ("Fireworks AI","greenhouse","fireworksai"),
    ("Lakeforge","greenhouse","lakeforge"),
    ("Sigma Computing","greenhouse","sigmacomputing"),
    ("Hightouch","greenhouse","hightouch"),
    ("LangChain","ashby","langchain"),
    ("Perplexity","ashby","perplexity"),
    ("Octave AI","ashby","octave"),
    ("Lexicon AI","ashby","lexicon"),
    ("Cascade AI","ashby","cascade"),
    ("Supabase","ashby","supabase"),
    ("WorkOS","ashby","workos"),
    ("Prefect","ashby","prefect"),
    ("Clay","ashby","claylabs"),
 # Fintech infra / billing / vertical AI
    ("Mercury","greenhouse","mercury"),
    ("Orb","ashby","orb"),
    ("Modern Treasury","ashby","moderntreasury"),
    ("Lago","ashby","lago"),
    ("Commure","ashby","commure"),
    ("Abridge","ashby","abridge"),
    ("Cohere Health","greenhouse","coherehealth"),
    ("Eve","greenhouse","eve"),
    ("Federato","greenhouse","federato"),
    ("Sixfold","greenhouse","sixfold"),
    ("Garner Health","greenhouse","garnerhealth"),
    ("Sayari","greenhouse","sayari"),
 # Dev-tools / data / remote-first
    ("dbt Labs","greenhouse","dbtlabsinc"),
    ("Webflow","greenhouse","webflow"),
    ("Grafana Labs","greenhouse","grafanalabs"),
    ("Calendly","greenhouse","calendly"),
    ("Vanta","ashby","vanta"),
    ("Render","ashby","render"),
    ("Haulix","ashby","haulix"),
 # Procurement / spend
    ("Fairmarkit","greenhouse","fairmarkit"),
    ("Zip","ashby","zip"),
 # On-domain (advanced mfg / CAD / industrial AI)
    ("SimScale","greenhouse","simscale"),
    ("Oden Technologies","ashby","oden-technologies"),
    ("Guidewheel","greenhouse","guidewheel"),
    ("AllSpice","ashby","allspice"),
    ("Rescale","ashby","rescale"),
    ("Flux.ai","ashby","flux"),
    ("Halcyon Manufacturing","ashby","halcyon"),
    ("Markforged","greenhouse","markforged"),
    ("AON3D","greenhouse","aon3d"),
    ("Quilter","ashby","quilter"),
    ("Zoo / KittyCAD","greenhouse","Zoo"),
    ("PhysicsX","greenhouse","physicsx"),
    ("Luminovo","ashby","luminovo"),
    ("Standard Bots","ashby","standardbots"),
    ("Vellum","ashby","vellum"),
 # AI-native / dev-tools / climate
    ("Anthropic","greenhouse","anthropic"),
    ("Cohere","greenhouse","cohere"),
    ("OpenAI","ashby","openai"),
    ("Watershed","ashby","watershed"),
    ("Linear","ashby","linear"),
    ("Replit","ashby","replit"),
    ("Together AI","ashby","togetherai"),
    ("Modal","ashby","modal"),
]

# Negative title markers => drop (core non-commercial functions).
NEG=["software engineer","ml engineer","machine learning","data engineer","research scientist",
     "researcher","infrastructure engineer","security engineer","devops","reliability engineer",
     "platform engineer","backend","frontend","full stack","full-stack","firmware","hardware engineer",
     "mechanical engineer","electrical engineer","controls engineer","technician","accountant",
     "controller","counsel","paralegal","recruit","sourcer","people partner","talent","payroll",
     "designer","ux ","ui ","brand","content writer","copywriter","social media","pr manager",
     "data scientist","analytics engineer","qa ","quality engineer","intern","sdr","bdr",
     "sales development","business development representative","executive assistant","office manager",
     "facilities","workplace","it support","helpdesk","biostatistician","clinical","nurse"]
# Positive title markers => keep (commercial/ops/CS/SE/PM/analyst/strategy/partner).
POS=["account executive","account manager","account director","revenue operations","revops",
     "sales operations","sales ops","gtm","go-to-market","commercial operations","business operations",
     "customer success","client success","client partner","customer engineer","success engineer",
     "solutions engineer","solutions architect","solutions consultant","sales engineer","value engineer",
     "forward deployed","applied ai","deployment strateg","technical account","implementation",
     "professional services","onboarding","partner manager","partnerships","alliances","channel",
     "product manager","principal product","group product","strategy","strateg","operations manager",
     "analyst","evangelist","field cto","head of sales","head of revenue","head of customer",
     "vp ","director","growth","quota","enterprise","strategic","commercial","renewals","expansion"]

REMOTE=["remote","anywhere","distributed"]
USST=["alabama","alaska","arizona","arkansas","california","colorado","connecticut","delaware","florida",
      "georgia","hawaii","idaho","illinois","indiana","iowa","kansas","kentucky","louisiana","maine",
      "maryland","massachusetts","michigan","minnesota","mississippi","missouri","montana","nebraska",
      "nevada","new hampshire","new jersey","new mexico","new york","north carolina","north dakota","ohio",
      "oklahoma","oregon","pennsylvania","rhode island","south carolina","south dakota","tennessee","texas",
      "utah","vermont","virginia","washington","west virginia","wisconsin","wyoming"]
USSIG=["united states","usa","u.s.","(us)","us-","-us","us remote","remote - us","remote, us","remote-us",
       "remote us","americas","north america"," us "]+USST
NONUS=["united kingdom","london","emea","apac","latam","canada","toronto","vancouver","ireland","dublin",
       "germany","berlin","munich","france","paris","spain","madrid","portugal","lisbon","poland","warsaw",
       "netherlands","amsterdam","india","bengaluru","bangalore","singapore","tokyo","japan","australia",
       "sydney","brazil","mexico","israel","haifa","tel aviv","european union","budapest","hungary","abu dhabi"]
TRAVEL=re.compile(r"(travel\s*(up to|of|approximately|~)?\s*\d{1,2}\s*%|\d{1,2}\s*%\s*travel|up to \d{1,2}% .{0,20}travel|\d{2}% .{0,20}on-site|driving (required|is required)|valid driver)", re.I)
# Hidden-travel language: events/conferences/on-site delivery/representation imply
# travel even when no "X%" appears. Treat as a travel deal-breaker (travel is a hard exclude). Flagged separately so it isn't missed by the %-regex.
HIDDEN_TRAVEL=re.compile(r"(represent[a-z ,']{0,30}(\bat\b|during|\bin\b)[a-z ,']{0,25}(event|conference|trade ?show|industry|academic)|attend[a-z ,']{0,20}(event|conference|trade ?show|summit)|speak[a-z ,']{0,15}(\bat\b|event|conference)|present[a-z ,']{0,20}(\bat\b|conference|trade ?show|event|in-?person)|on-?site (training|delivery|deliver|workshop|visit|session)|deliver[a-z ,']{0,20}(on-?site|in-?person|in person)|in-?person (training|delivery|workshop|session|meeting)|customer site|trade ?show|booth|symposium|user conference|field-?based|in (the )?office|hub-?based|\d{1,2}\s*days?\s*(a|per)\s*week\s*in\s*(the\s*)?office|expect[a-z ,']{0,20}in\s*(the\s*)?office|in[- ]office\s*(\d{1,2}|expect|requirement|required))", re.I)
def hidden_travel_flag(desc):
    m=HIDDEN_TRAVEL.search(desc or "")
    return m.group(0).strip() if m else ""

def get(url):
    req=Request(url,headers={"User-Agent":UA,"Accept":"application/json"})
    with urlopen(req,timeout=20,context=_SSL) as r: return json.loads(r.read().decode())
def strip(t):
    if not t: return ""
    return re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]+>"," ",t))).strip()
def title_ok(t):
    tl=t.lower()
    if any(n in tl for n in NEG): return False
    return any(p in tl for p in POS)
_BARE_US={"us","usa","u.s.","u.s.a.","united states","united states of america"}
_REMOTE_BODY=re.compile(r"(this (?:position|role) is remote|fully[- ]remote|remote[- ]first|remote position|us[- ]remote|remote,?\s+us\b|remote in the (?:us|united states)|work from anywhere in the (?:us|united states))", re.I)
def remote_us(loc, is_remote=False):
    l=(loc or "").lower().strip()
    nonus=any(n in l for n in NONUS); us=any(s in l for s in USSIG) or l in _BARE_US; rem=any(r in l for r in REMOTE) or is_remote
    if nonus and not us: return False
    return rem and (us or "remote" in l or is_remote)
def travel_flag(desc):
    m=TRAVEL.search(desc or "")
    return m.group(0) if m else ""

def gh(slug,cut):
    out=[]
    for j in ats_client.fetch_greenhouse(slug, get=get):
        t=j.get("title","")
        if not title_ok(t): continue
        loc=(j.get("location") or {}).get("name","") if isinstance(j.get("location"),dict) else ""
        desc=strip(j.get("content",""))
 # Greenhouse has no is_remote field — derive from JD body when location is a bare country code
        if not remote_us(loc, bool(_REMOTE_BODY.search(desc))): continue
        upd=j.get("updated_at") or j.get("first_published"); dt=None
        if upd:
            try: dt=datetime.fromisoformat(upd.replace("Z","+00:00"))
            except Exception as _e:
                try:
                    import obs; obs.capture(_e, where="source_deep:greenhouse:date", payload={"upd": upd})
                except Exception: pass
        if dt and dt<cut: continue
        out.append({"title":t,"loc":loc,"date":dt.date().isoformat() if dt else None,
                    "url":j.get("absolute_url",""),"travel":travel_flag(desc),
                    "hidden":hidden_travel_flag(desc),"comp":comp_scan(desc)})
    return out
def ash(slug,cut):
    out=[]
    for j in ats_client.fetch_ashby(slug, get=get):
        if not j.get("isListed",True): continue
        t=j.get("title","")
        if not title_ok(t): continue
        locs=[j.get("location") or ""]+[(s.get("location") or "") for s in (j.get("secondaryLocations") or [])]
        loc=" / ".join(x for x in locs if x)
        if not remote_us(loc, bool(j.get("isRemote"))): continue
        pub=j.get("publishedAt"); dt=None
        if pub:
            try: dt=datetime.fromisoformat(pub.replace("Z","+00:00"))
            except Exception as _e:
                try:
                    import obs; obs.capture(_e, where="source_deep:ashby:date", payload={"pub": pub})
                except Exception: pass
        if dt and dt<cut: continue
        desc=strip(j.get("descriptionPlain") or j.get("descriptionHtml",""))
        comp=j.get("compensation") or {}; band=""
        for c in (comp.get("summaryComponents") or []):
            if (c.get("compensationType") or "")=="Salary":
                band=f"${c.get('minValue')}-{c.get('maxValue')}"; break
        out.append({"title":t,"loc":loc,"date":dt.date().isoformat() if dt else None,
                    "url":j.get("jobUrl",""),"travel":travel_flag(desc),
                    "hidden":hidden_travel_flag(desc),"comp":band or comp_scan(desc)})
    return out
def _postjson(url,payload):
    req=Request(url,data=json.dumps(payload).encode(),
                headers={"User-Agent":UA,"Accept":"application/json","Content-Type":"application/json"})
    with urlopen(req,timeout=25,context=_SSL) as r: return json.loads(r.read().decode())
def wd(slug,cut):
 # Workday CXS board sweep. slug = "tenant/wdhost/site" e.g. "boreal/wd1/Boreal".
 # POST the jobs list (filter remote-US on locationsText, cheap), then GET each
 # survivor's detail for the body. Extends deep-fetch to custom-ATS industrial cos.
    out=[]
    tenant,wdhost,site=slug.split("/")
    base=f"https://{tenant}.{wdhost}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    data=_postjson(f"{base}/jobs",{"limit":20,"offset":0,"searchText":"","appliedFacets":{}})
    for p in data.get("jobPostings",[]):
        t=p.get("title","")
        if not title_ok(t): continue
        loc=p.get("locationsText","")
        if not remote_us(loc): continue
        path=p.get("externalPath",""); desc=""
        url=f"https://{tenant}.{wdhost}.myworkdayjobs.com/{site}{path}"
        try:
            info=get(f"{base}{path}").get("jobPostingInfo",{})
            desc=strip(info.get("jobDescription","")); url=info.get("externalUrl") or url
        except Exception as _e:
            try:
                import obs; obs.capture(_e, where="source_deep:workday:detail", payload={"slug": slug})
            except Exception: pass
        out.append({"title":t,"loc":loc,"date":None,"url":url,
                    "travel":travel_flag(desc),"hidden":hidden_travel_flag(desc),"comp":comp_scan(desc)})
        time.sleep(0.1)
    return out
def comp_scan(desc):
    m=re.search(r"\$[0-9]{2,3},[0-9]{3}\s*[-–to]+\s*\$?[0-9]{2,3},[0-9]{3}",desc or "")
    return m.group(0) if m else ""

def run(since=30):
    cut=datetime.now(timezone.utc)-timedelta(days=since)
    res={}
    for name,ats,slug in BOARDS:
        try:
            rows=(gh(slug,cut) if ats=="greenhouse"
                  else ash(slug,cut) if ats=="ashby"
                  else wd(slug,cut) if ats=="workday"
                  else [])
        except Exception as e:
            res[name]={"error":str(e)[:50]}; continue
        res[name]=rows
        time.sleep(0.2)
    print(json.dumps({"cutoff":cut.date().isoformat(),"boards":res},indent=1))

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--since",type=int,default=30); a=ap.parse_args()
    run(a.since)
