import requests
import pandas as pd
from io import StringIO

def download_and_combine_csv(base_url, file_names, combined_file_name):
    # Initialize an empty DataFrame to hold all the data
    all_data = pd.DataFrame()
    
    for file_name in file_names:
        # Construct file URL
        file_url = base_url + file_name
    
        # Send request to get the file content
        response = requests.get(file_url)
        if response.status_code == 200:
            # Use StringIO to read CSV content into DataFrame
            data = StringIO(response.text)
            df = pd.read_csv(data)
    
            # Concatenate this DataFrame with the overall DataFrame
            all_data = pd.concat([all_data, df])
        else:
            print(f"Failed to download {file_name}")
    
    # Save the concatenated DataFrame in Feather format
    feather_file = 'combined_sales_data.feather'
    all_data.to_feather(feather_file)
    print("All data saved in Feather file.")
