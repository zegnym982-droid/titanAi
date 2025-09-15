#!/usr/bin/env python3
"""
Test script for Google Sheets connectivity
"""

import json
import os
import tempfile
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials


def resolve_sa_key_path():
    """Resolve service account key path from environment"""
    # Check if we have a direct path
    if 'GOOGLE_SERVICE_ACCOUNT_JSON' in os.environ:
        sa_path = os.environ['GOOGLE_SERVICE_ACCOUNT_JSON']
        if os.path.exists(sa_path):
            return sa_path
            
    # Check for inline JSON in SERVICE_ACCOUNT_JSON
    if 'SERVICE_ACCOUNT_JSON' in os.environ:
        sa_json = os.environ['SERVICE_ACCOUNT_JSON']
        try:
            # Validate JSON
            json.loads(sa_json)
            # Write to temp file
            temp_path = '/tmp/sa_test.json'
            with open(temp_path, 'w') as f:
                f.write(sa_json)
            os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'] = temp_path
            return temp_path
        except json.JSONDecodeError:
            print("❌ Invalid JSON in SERVICE_ACCOUNT_JSON")
            
    raise ValueError("No valid Google service account credentials found")


def test_google_sheets():
    """Test Google Sheets access and create a test sheet"""
    try:
        print("🔧 Testing Google Sheets connection...")
        
        # Set up credentials
        sa_path = resolve_sa_key_path()
        scopes = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/drive.file'
        ]
        
        credentials = ServiceAccountCredentials.from_json_keyfile_name(sa_path, scopes)
        client = gspread.authorize(credentials)
        
        print("✅ Successfully authenticated with Google Sheets API")
        
        # Create test spreadsheet
        test_title = f"Fitness Bot Test - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        spreadsheet = client.create(test_title)
        
        print(f"✅ Created test spreadsheet: {test_title}")
        print(f"📊 Spreadsheet URL: https://docs.google.com/spreadsheets/d/{spreadsheet.id}")
        
        # Add test data
        worksheet = spreadsheet.sheet1
        worksheet.update_title("Test Data")
        
        headers = ["Date", "Exercise", "Weight", "Reps", "Notes"]
        worksheet.update('A1:E1', [headers])
        
        test_data = [
            [datetime.now().strftime('%Y-%m-%d'), "Test Exercise", 100, 5, "Test entry"]
        ]
        worksheet.append_row(test_data[0])
        
        print("✅ Added test data to spreadsheet")
        
        # Test reading data
        all_records = worksheet.get_all_records()
        print(f"✅ Successfully read {len(all_records)} records from spreadsheet")
        
        # Share spreadsheet (make it viewable)
        try:
            spreadsheet.share('', perm_type='anyone', role='reader')
            print("✅ Made spreadsheet publicly viewable")
        except Exception as e:
            print(f"⚠️ Could not make spreadsheet public: {e}")
        
        print("\n🎉 Google Sheets test completed successfully!")
        print(f"📋 Test spreadsheet ID: {spreadsheet.id}")
        print(f"🔗 Access it at: https://docs.google.com/spreadsheets/d/{spreadsheet.id}")
        
        return spreadsheet.id
        
    except Exception as e:
        print(f"❌ Google Sheets test failed: {e}")
        print("\nTroubleshooting:")
        print("1. Check that SERVICE_ACCOUNT_JSON is set correctly in Replit Secrets")
        print("2. Verify that the service account has Google Sheets and Drive API access")
        print("3. Ensure the JSON key is valid and not expired")
        raise


if __name__ == "__main__":
    test_google_sheets()