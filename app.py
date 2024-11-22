from flask import Flask, render_template, request, send_file, session, flash
import pandas as pd
import io
import os
import requests
import json
from datetime import datetime
from dotenv import load_dotenv

app = Flask(__name__)
app.secret_key = os.urandom(24)

load_dotenv()

def load_mapping():
    try:
        # Get the absolute path to the mapping file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        mapping_file_path = os.path.join(current_dir, 'mapping.csv')
        
        print(f"Attempting to load mapping file from: {mapping_file_path}")
        
        # Check if file exists
        if not os.path.exists(mapping_file_path):
            raise FileNotFoundError(f"mapping.csv not found at {mapping_file_path}")
        
        # Read the mapping file
        mapping_df = pd.read_csv(mapping_file_path)
        
        # Verify the required columns exist
        required_columns = ['Expense Category Name', 'xero code']
        if not all(col in mapping_df.columns for col in required_columns):
            raise ValueError(f"Mapping file must contain columns: {required_columns}")
        
        # Clean the xero codes: strip whitespace and handle NaN values
        mapping_df['xero code'] = mapping_df['xero code'].astype(str).str.strip()
        
        # Create a dictionary from the mapping file
        mapping_dict = dict(zip(mapping_df['Expense Category Name'], mapping_df['xero code']))
        
        # Remove any entries where xero code is empty or just whitespace
        mapping_dict = {k: v for k, v in mapping_dict.items() 
                       if v is not None and v.strip() != '' and v.strip() != 'nan'}
        
        print(f"Successfully loaded {len(mapping_dict)} mappings")
        return mapping_dict
        
    except FileNotFoundError as e:
        print(f"FileNotFoundError: {str(e)}")
        raise Exception("Mapping file not found. Please ensure 'mapping.csv' is in the same directory as app.py")
    except Exception as e:
        print(f"Error loading mapping file: {str(e)}")
        raise Exception(f"Error loading mapping file: {str(e)}")

def process_expense_file(df):
    try:
        # Load the mapping dictionary
        mapping_dict = load_mapping()
        
        if not mapping_dict:
            raise Exception("Mapping file is empty or could not be loaded")
        
        # Verify required columns exist in the uploaded file
        required_columns = ['Employee Name', 'Report Number', 'Submitted Date', 
                          'Expense Amount (in Reimbursement Currency)', 'Expense Description', 'Tax Code']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            raise Exception(f"Missing required columns: {', '.join(missing_columns)}")
        
        # Create new DataFrame with mapped columns
        processed_df = pd.DataFrame()
        processed_df['ContactName'] = df['Employee Name']
        processed_df['InvoiceNumber'] = df['Report Number']
        processed_df['InvoiceDate'] = df['Created Time']
        processed_df['DueDate'] = df['Created Time']
        processed_df['Description'] = df['Expense Description']
        processed_df['Quantity'] = 1
        processed_df['UnitAmount'] = df['Expense Amount (in Reimbursement Currency)']
        
        # Map the Xero codes and convert to integer where possible
        processed_df['AccountCode'] = df['Expense Category'].map(mapping_dict)
        
        # Convert AccountCode to integer, handling non-numeric values
        def convert_to_int(value):
            try:
                # Extract first number from string if it contains one
                import re
                numbers = re.findall(r'\d+', str(value))
                return int(numbers[0]) if numbers else 0
            except:
                return 0
        
        processed_df['AccountCode'] = processed_df['AccountCode'].apply(convert_to_int)
        
        # Add TaxType column based on Tax Code
        processed_df['TaxType'] = df['Tax Code'].apply(
            lambda x: 'GST on Expenses' if x == 'GST' else 'GST Free Expenses'
        )
        
        # Identify unmapped categories
        unmapped_categories = df[processed_df['AccountCode'] == 0]['Expense Category'].unique()
        if len(unmapped_categories) > 0:
            print(f"Warning: Unmapped categories found: {unmapped_categories}")
        
        # Ensure all columns are present even if empty
        required_output_columns = [
            'ContactName', 'InvoiceNumber', 'InvoiceDate', 'DueDate',
            'Description', 'Quantity', 'UnitAmount', 'AccountCode', 'TaxType'
        ]
        for col in required_output_columns:
            if col not in processed_df.columns:
                processed_df[col] = ''
        
        # Reorder columns
        processed_df = processed_df[required_output_columns]
        
        return processed_df, unmapped_categories
    except Exception as e:
        raise Exception(f"Error processing file: {str(e)}")

def send_to_xero(df):
    try:
        url = "https://api.xero.com/api.xro/2.0/Invoices"
        
        xero_token = os.getenv('XERO_ACCESS_TOKEN')
        if not xero_token:
            raise Exception("Xero access token not found in environment variables")
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {xero_token}"
        }
        
        # Group by InvoiceNumber to create separate bills
        grouped = df.groupby('InvoiceNumber')
        responses = []
        
        for invoice_num, group in grouped:
            line_items = []
            for _, row in group.iterrows():
                line_item = {
                    "Description": row['Description'],
                    "Quantity": float(row['Quantity']),
                    "UnitAmount": float(row['UnitAmount']),
                    "AccountCode": str(row['AccountCode']),
                    "TaxType": row['TaxType']
                }
                line_items.append(line_item)
            
            bill_data = {
                "Type": "ACCPAY",
                "Contact": {
                    "Name": group['ContactName'].iloc[0]
                },
                "LineItems": line_items,
                "Date": group['InvoiceDate'].iloc[0],
                "DueDate": group['DueDate'].iloc[0],
                "Status": "AUTHORISED",
                "InvoiceNumber": invoice_num
            }
            
            response = requests.post(url, headers=headers, data=json.dumps(bill_data))
            responses.append({
                'invoice': invoice_num,
                'status': response.status_code,
                'message': response.text if response.status_code != 200 else 'Success'
            })
        
        return responses
    except Exception as e:
        raise Exception(f"Error sending to Xero: {str(e)}")

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file uploaded')
            return render_template('index.html', error="No file uploaded")
        
        file = request.files['file']
        if file.filename == '':
            flash('No file selected')
            return render_template('index.html', error="No file selected")
        
        if file and file.filename.endswith('.csv'):
            try:
                # Read the CSV file
                df = pd.read_csv(file)
                
                # Process the DataFrame
                processed_df, unmapped_categories = process_expense_file(df)
                
                # Store processed data in session
                session['processed_data'] = processed_df.to_json()
                
                # Convert to HTML table for preview
                table_html = processed_df.to_html(classes='data-table', index=False)
                
                # Prepare warning message for unmapped categories
                warning_message = None
                if len(unmapped_categories) > 0:
                    warning_message = f"Warning: Found unmapped categories: {', '.join(unmapped_categories)}"
                
                return render_template('index.html', 
                                     table_html=table_html, 
                                     show_download=True,
                                     warning_message=warning_message)
            
            except Exception as e:
                return render_template('index.html', error=str(e))
            
        return render_template('index.html', error="Invalid file format")
    
    return render_template('index.html')

@app.route('/download')
def download():
    if 'processed_data' not in session:
        flash('No processed data available')
        return render_template('index.html', error="No processed data available")
    
    try:
        # Recover the DataFrame from session
        df = pd.read_json(session['processed_data'])
        
        # Save to buffer
        buffer = io.StringIO()
        df.to_csv(buffer, index=False)
        buffer.seek(0)
        
        return send_file(
            io.BytesIO(buffer.getvalue().encode()),
            mimetype='text/csv',
            as_attachment=True,
            download_name='xero_import.csv'
        )
    except Exception as e:
        flash('Error downloading file')
        return render_template('index.html', error=str(e))

@app.route('/send-to-xero')
def send_to_xero_route():
    if 'processed_data' not in session:
        return {'error': 'No processed data available'}, 400
    
    try:
        df = pd.read_json(session['processed_data'])
        responses = send_to_xero(df)
        return {'status': 'success', 'responses': responses}
    except Exception as e:
        return {'error': str(e)}, 500

if __name__ == '__main__':
    app.run(debug=True) 