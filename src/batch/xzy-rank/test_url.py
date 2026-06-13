"""Test URL generation for XZY ranking API."""

import requests
from urllib.parse import urlencode

# Constants from app.py
API_BASE_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/hot-rank"

def test_url_generation():
    """Test and display the generated URL."""
    list_id = 106
    
    params = {
        "tt_type": "2v2",
        "tt_score": "≥6000，＜8000",  # 全角カンマと全角< が必要
        "order_field": "win_rate",
        "order_method": "DESC",
        "list_id": list_id,
    }
    
    # Generate URL
    full_url = f"{API_BASE_URL}?{urlencode(params)}"
    
    print("=" * 80)
    print("Generated URL:")
    print("=" * 80)
    print(full_url)
    print()
    
    print("=" * 80)
    print("Parameters:")
    print("=" * 80)
    for key, value in params.items():
        print(f"  {key}: {value}")
    print()
    
    # Test actual request
    print("=" * 80)
    print("Testing actual API request...")
    print("=" * 80)
    try:
        response = requests.get(API_BASE_URL, params=params, timeout=10)
        print(f"Status Code: {response.status_code}")
        print(f"Response URL: {response.url}")
        print()
        
        if response.status_code == 200:
            data = response.json()
            print(f"Response Code: {data.get('code')}")
            print(f"Message: {data.get('msg')}")
            print(f"Data Count: {len(data.get('data', []))}")
            
            if data.get('data'):
                print("\nFirst item preview:")
                first_item = data['data'][0]
                role = first_item.get('role', {})
                print(f"  Name: {role.get('name_jp')}")
                print(f"  Win Rate: {first_item.get('win_rate')}%")
                print(f"  On Rate: {first_item.get('on_rate')}%")
                print(f"  Ban Rate: {first_item.get('ban_rate')}%")
        else:
            print(f"Error: {response.text}")
    
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_url_generation()
