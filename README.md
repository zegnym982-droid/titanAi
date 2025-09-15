# Fitness Logger Telegram Bot

A comprehensive Telegram bot for tracking workouts with Google Sheets integration, automated scheduling, and intelligent training recommendations.

## Features

- 🏋️ **Workout Tracking**: Start workouts with `/train`, add sets with flexible parsing, finish with sRPE/mood/sleep feedback
- 📊 **Google Sheets Integration**: Personal spreadsheet per user with automatic data sync
- 🤖 **Smart Recommendations**: Training load analysis and automatic workout recommendations
- 📅 **Automated Scheduling**: Weekly reports every Sunday 20:00 and daily backups at 02:30 UTC
- 🎤 **Voice Notes**: Transcription storage (expandable for future Whisper integration)
- 📈 **Progress Analysis**: Weekly statistics, tonnage calculation, and overload detection

## Quick Start on Replit

### 1. Add Required Secrets

Go to **Tools → Secrets** in your Replit and add:

#### `TELEGRAM_BOT_TOKEN`
1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Send `/newbot` and follow the instructions
3. Copy the bot token and paste it here

#### `SERVICE_ACCOUNT_JSON`
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable Google Sheets API and Google Drive API
4. Create a service account:
   - Go to **IAM & Admin → Service Accounts**
   - Click **Create Service Account**
   - Give it a name (e.g., "fitness-bot")
   - Click **Create and Continue**
   - Skip role assignment and click **Done**
5. Create a key:
   - Click on your service account
   - Go to **Keys** tab
   - Click **Add Key → Create new key**
   - Choose **JSON** format
   - Download the file
6. Copy the **entire contents** of the JSON file and paste it as the value

### 2. Run the Bot

Simply click the **▶️ Run** button in Replit!

The bot will:
- Start a keep-alive HTTP server on the configured port
- Initialize the SQLite database
- Connect to Google Sheets
- Begin polling for Telegram messages

### 3. Test Your Bot

1. Find your bot on Telegram (search for the name you gave it)
2. Send `/start` to create your personal fitness spreadsheet
3. Try the workflow:
   ```
   /start          # Create account and spreadsheet
   /train          # Start a workout
   /add Bench 60x5x3  # Add sets (flexible format)
   /finish         # Complete workout with feedback
   /week           # View weekly report
   ```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Create account and personal Google Sheets |
| `/train` | Start a new workout session |
| `/add <sets>` | Add sets to current workout |
| `/finish` | Complete workout with sRPE/mood/sleep rating |
| `/week` | Generate weekly progress report |

## Set Format Examples

The bot accepts multiple formats for adding sets:

```
Bench 60x5x3        # Exercise, weight×reps×sets
Жим 80x3@8          # Russian names, weight×reps@RPE
Deadlift 120x5      # Single set
Squat 100x3x5@7     # Multiple sets with RPE
```

### Supported Exercises (Russian/English)
- Жим / Bench → Bench Press
- Присед / Squat → Squat  
- Тяга / Deadlift → Deadlift
- Жим стоя / OHP → Overhead Press

## Google Sheets Structure

Your personal spreadsheet contains 5 sheets:

1. **Workouts**: Date, program, duration, sRPE, mood, sleep, training load
2. **Sets**: Individual set data with exercise, weight, reps, RPE
3. **PRs**: Personal records tracking
4. **Weekly**: Weekly summary statistics
5. **Notes**: Voice transcriptions and workout notes

## Automated Features

- **Weekly Reports**: Every Sunday 20:00 (Europe/Amsterdam timezone)
- **Daily Backups**: Every day at 02:30 UTC, exports sheets to CSV
- **Training Load Analysis**: Compares weekly load vs 4-week average
- **Smart Recommendations**: Based on sRPE, sleep quality, and load patterns

## Troubleshooting

### Bot doesn't respond
1. Check that `TELEGRAM_BOT_TOKEN` is correctly set
2. Verify the bot token is valid in BotFather
3. Check the console logs for errors

### Google Sheets errors
1. Run the test script: `python test_gsheets.py`
2. Verify `SERVICE_ACCOUNT_JSON` contains valid JSON
3. Check that Google Sheets API and Drive API are enabled
4. Ensure the service account key hasn't expired

### Spreadsheet access
To view your spreadsheet in Google Sheets:
1. Copy the service account email from the JSON (looks like `xyz@project.iam.gserviceaccount.com`)
2. Share your spreadsheet with this email address
3. Or the bot automatically makes sheets publicly viewable

## Security Notes

- **Never commit secrets**: Bot tokens and service account keys should only be in Replit Secrets
- **Rotate keys regularly**: If keys are compromised, revoke them immediately
- **Monitor access**: Check your Google Cloud console for unusual API usage

## File Structure

```
fitness-bot/
├── main.py              # Main bot application
├── requirements.txt     # Python dependencies  
├── test_gsheets.py     # Google Sheets test script
├── README.md           # This file
├── fitness_bot.db      # SQLite database (auto-created)
└── backups/            # CSV backups (auto-created)
```

## Development

The bot uses:
- **aiogram 3.x**: Modern async Telegram bot framework
- **gspread**: Google Sheets Python API
- **APScheduler**: Automated task scheduling
- **SQLite**: Local database for fast queries
- **pandas**: Data analysis and CSV export

## Support

If you encounter issues:
1. Check the console logs in Replit
2. Run `python test_gsheets.py` to test Google Sheets connectivity
3. Verify all secrets are properly configured
4. Check that your Telegram bot token is valid

---

**Ready to start logging your fitness journey! 💪**