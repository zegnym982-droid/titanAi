# AI-Powered Fitness Coach Telegram Bot

## Overview

The AI-Powered Fitness Coach is a comprehensive Telegram bot that combines intelligent workout tracking with ChatGPT-powered coaching. The bot provides personalized workout program generation, smart workout parsing through AI, automated scheduling for reminders and reports, and intelligent training recommendations. All data is stored locally in SQLite with no external dependencies required.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Bot Framework
- **Technology**: aiogram v3 for Telegram Bot API integration
- **Architecture Pattern**: Event-driven command handling with async/await patterns
- **Deployment**: Replit hosting with polling mode and HTTP keep-alive server for continuous operation

### Data Storage Strategy
- **Primary Storage**: Comprehensive SQLite database for all user data and workout tracking
- **AI Integration**: ChatGPT for workout program generation and intelligent workout parsing
- **Backup System**: Automated daily database backups and CSV export functionality

### Database Schema Design
- **Users Table**: Stores user preferences (timezone, goals, experience, equipment, injuries, schedule_days, preferred_time_local)
- **Workouts Table**: Session-level data (duration, sRPE, fatigue, sleep_quality, training load, program_id)
- **Sets Table**: Exercise-specific data (weight, reps, RPE, comments) with workout linking
- **Workout Programs Table**: AI-generated and user-uploaded workout programs
- **Workout Timer Table**: Active workout timers and completion notifications
- **PRs Table**: Personal records tracking
- **Weekly Stats Table**: Aggregated weekly training statistics
- **Notes Table**: Voice notes and workout commentary

### Google Sheets Integration
- **Authentication**: OAuth2 service account with JSON key credentials
- **Sheet Structure**: Multi-tab design (Workouts, Sets, PRs, Weekly, Notes) with standardized headers
- **Scopes**: Full Sheets and Drive API access for creation, modification, and sharing
- **Data Sync**: Real-time updates during workout sessions with batch operations for performance

### Automated Scheduling System
- **Scheduler**: APScheduler with async support for non-blocking operations
- **Weekly Reports**: Sunday 20:00 Europe/Amsterdam timezone for consistency
- **Daily Backups**: 02:30 UTC to avoid peak usage times
- **Training Recommendations**: Automated deload suggestions based on 4-week rolling averages

### Workout Tracking Flow
- **Session Management**: State-driven workflow from start to finish
- **Set Parsing**: Flexible text parsing for weight/reps/RPE input formats
- **Feedback Collection**: Post-workout sRPE, mood, and sleep quality ratings
- **Load Calculation**: Training load = sRPE × duration for objective quantification

### Voice Note Integration
- **Current State**: Transcript storage infrastructure ready
- **Future Expansion**: Designed for Whisper API integration
- **Data Linking**: Voice notes connected to specific workouts for context

### Training Intelligence
- **Load Monitoring**: Weekly training load analysis with trend detection
- **Deload Logic**: Automatic recommendations when load exceeds 25% of 4-week average
- **Progress Tracking**: Tonnage calculations and personal record detection
- **Report Generation**: Automated weekly statistics with actionable insights

## External Dependencies

### Telegram Integration
- **Bot API**: Telegram Bot API via aiogram v3 framework
- **Authentication**: Bot token from @BotFather for API access

### Google Cloud Platform
- **Sheets API**: Google Sheets API v4 for spreadsheet operations
- **Drive API**: Google Drive API for sheet creation and sharing permissions
- **Authentication**: Service account with JSON key for server-to-server access
- **Required Scopes**: Sheets, Drive, and Drive File permissions

### Python Libraries
- **aiogram**: Telegram bot framework with async support
- **gspread**: Google Sheets Python client library
- **oauth2client**: Google OAuth2 authentication handling
- **APScheduler**: Advanced Python scheduler for automated tasks
- **pandas**: Data analysis and CSV operations
- **python-dateutil**: Flexible date parsing and timezone handling
- **pytz**: Timezone calculations and conversions

### Infrastructure Requirements
- **Replit Hosting**: Container-based hosting with persistent storage
- **HTTP Server**: Keep-alive server to prevent container sleep
- **File System**: Local backup storage with automatic directory creation
- **Environment Variables**: Secure secret management through Replit Secrets

### Configuration Management
- **Environment Variables**: TELEGRAM_BOT_TOKEN, SERVICE_ACCOUNT_JSON
- **Default Settings**: Europe/Amsterdam timezone, metric units (kg)
- **Backup Location**: Configurable backup directory with fallback defaults