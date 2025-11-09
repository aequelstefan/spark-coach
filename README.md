# Spark Coach - AI Social Media Growth Engine

Autonomous AI system that drives X growth through intelligent content generation, opportunity detection, and data-driven optimization.

## Quick Start

### 1. One-Time Setup (10 minutes)
```bash
# Install dependencies
pip install -r requirements.txt

# Run voice calibration
python coach.py --task setup
```

Answer 8 questions in Slack to teach the system your voice and what to write about.

### 2. Daily Usage (5 minutes)

**Morning (08:30 CET):**
- Coaching card + 3 tweet options
- React 1️⃣2️⃣3️⃣ to post your pick

**Throughout day:**
- Urgent alerts for AMAs/viral threads; react 👍 to draft and ✅ to post

**Evening (19:00 CET):**
- Daily analytics + learning insights

**Sunday (20:00 CET):**
- Weekly brief
- Answer: "What did you ship this week?" (2-min refresh)

### 3. Commands

```bash
python coach.py --task setup      # Voice calibration (one-time)
python coach.py --task suggest    # Morning session
python coach.py --task scan       # Opportunity scan
python coach.py --task summary    # Daily analytics
python coach.py --task refresh    # Update weekly context
```

## Key Features

- 95% time savings: 2-3 hours/day → 5 minutes/day
- Voice calibration: Learns YOUR voice and topics
- Daily coaching: Tells you what to focus on
- Smart learning: Adapts based on what works
- Cost: $9-12/month (Claude API only)

## Documentation

- PRD.md: Complete product requirements and technical specs
- WARP.md: AI assistant development guide
- creators.json: 51 curated accounts across 3 tiers

## Architecture

```
GitHub Actions (scheduled) → coach.py → Claude + X + Slack APIs
                                ↓
                         data/ (JSONL files)
                         - voice_profile.json (your calibration)
                         - log.jsonl (event stream)
                         - metrics.jsonl (performance)
                         - learning.json (optimization state)
```

## Support

Questions? Check PRD.md sections 9-10 for troubleshooting and manual commands.
