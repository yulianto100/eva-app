import gspread
from google.oauth2.service_account import Credentials

SERVICE_ACCOUNT_FILE = r"D:\Master-app\converter\eva-converter\.secrets\eva-google-service-account.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_IDS = [
    "1QUW7iwJRSHKZi4NTdpZrxCMUugeiS4uC8qUFTjLIQWc",
    "1eN6WVXxUtMkJQRNlKMHvRNUgfzs4VMdMm5yc6Wku2bs",
]

def main():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=SCOPES,
    )
    client = gspread.authorize(creds)

    for spreadsheet_id in SPREADSHEET_IDS:
        spreadsheet = client.open_by_key(spreadsheet_id)
        print(f"\nSpreadsheet: {spreadsheet.title}")
        print(f"ID: {spreadsheet_id}")
        print("Worksheets:")
        for ws in spreadsheet.worksheets():
            print(f"- {ws.title} (rows={ws.row_count}, cols={ws.col_count})")

if __name__ == "__main__":
    main()