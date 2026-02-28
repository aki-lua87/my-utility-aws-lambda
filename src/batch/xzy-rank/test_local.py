"""Local test script for XZY ranking function.

This script allows you to test the Lambda function locally without AWS.
"""

import os
import sys

# Mock environment variables for local testing
os.environ["TABLE_NAME"] = "aki-utils-dev"
os.environ["XZY_WEBHOOK_URL"] = "YOUR_DISCORD_WEBHOOK_URL_HERE"  # Replace with actual webhook URL
os.environ["BEDROCK_ANALYSIS_ENABLED"] = "true"  # Set to "true" to test Bedrock analysis

# Mock AWS SDK if not available locally
try:
    import boto3
except ImportError:
    print("Warning: boto3 not installed. Install with: pip install boto3")
    sys.exit(1)

# Import the Lambda handler
from app import lambda_handler, fetch_ranking_data, format_ranking_message, generate_ranking_images, analyze_with_bedrock


def test_fetch_data():
    """Test fetching data from API."""
    print("=" * 60)
    print("Test 1: Fetching data from API")
    print("=" * 60)
    
    # Test with list_id 106
    print("\nFetching list_id 106...")
    data = fetch_ranking_data(106)
    
    if data and data.get("code") == 0:
        print(f"✓ Success! Found {len(data.get('data', []))} items")
        if data.get("data"):
            print("\nSample data (first item):")
            first_item = data["data"][0]
            role = first_item.get("role", {})
            print(f"  Name: {role.get('name_jp')}")
            print(f"  Win Rate: {first_item.get('win_rate')}%")
            print(f"  On Rate: {first_item.get('on_rate')}%")
            print(f"  Ban Rate: {first_item.get('ban_rate')}%")
    else:
        print("✗ Failed to fetch data")
    
    return data


def test_format_message(data):
    """Test formatting message for Discord."""
    print("\n" + "=" * 60)
    print("Test 2: Formatting Discord message")
    print("=" * 60)
    
    if not data or not data.get("data"):
        print("✗ No data to format")
        return
    
    embeds = format_ranking_message(data["data"], 106)
    
    print(f"\n✓ Discord Embeds Generated: {len(embeds)} embed(s)")
    
    for idx, embed in enumerate(embeds, 1):
        print(f"\n  Embed {idx}:")
        print(f"    Title: {embed['title']}")
        print(f"    Color: #{embed['color']:06x}")
        if 'footer' in embed:
            print(f"    Footer: {embed['footer']['text']}")
        print("\n    Description Preview:")
        description_lines = embed["description"].split("\n")[:3]
        for line in description_lines:
            print(f"      {line}")
        total_lines = len(embed["description"].split("\n"))
        if total_lines > 3:
            print(f"      ... and {total_lines - 3} more lines")
        print(f"    Total characters: {len(embed['description'])}")
    
    # Show total data count
    total_items = sum(len(e["description"].split("\n")) for e in embeds)
    print(f"\n  Total items displayed: {total_items}")
    print(f"  Original data count: {len(data['data'])}")


def test_generate_images(data):
    """Test generating ranking images."""
    print("\n" + "=" * 60)
    print("Test 3: Generating Ranking Images")
    print("=" * 60)
    
    if not data or not data.get("data"):
        print("✗ No data to generate images")
        return
    
    # Sort by on_rate
    sorted_data = sorted(data["data"], key=lambda x: float(x.get("on_rate", 0)), reverse=True)
    
    print("\n⏳ Generating images (this may take a while)...")
    images = generate_ranking_images(sorted_data)
    
    print(f"\n✓ Generated {len(images)} image(s)")
    
    for idx, (filename, img) in enumerate(images, 1):
        print(f"\n  Image {idx}:")
        print(f"    Filename: {filename}")
        print(f"    Size: {img.size[0]}x{img.size[1]} pixels")
        print(f"    Format: {img.format or 'PNG'}")
        
        # Save image for manual inspection
        output_path = f"test_output_{filename}"
        img.save(output_path)
        print(f"    Saved to: {output_path}")
    
    print(f"\n✓ All images saved successfully!")


def test_bedrock_analysis(list_id: int):
    """Test Bedrock analysis by comparing list_id vs list_id - 1 from API."""
    print("\n" + "=" * 60)
    print("Test 4: Bedrock Analysis (current vs previous list_id)")
    print("=" * 60)

    if os.environ.get("BEDROCK_ANALYSIS_ENABLED") != "true":
        print("\n⚠️  BEDROCK_ANALYSIS_ENABLED is not set to 'true'")
        print("   Edit test_local.py and set BEDROCK_ANALYSIS_ENABLED = 'true' to run this test")
        return

    prev_list_id = list_id - 1
    print(f"\n  Current  list_id: {list_id}")
    print(f"  Previous list_id: {prev_list_id}")

    print(f"\n⏳ Fetching current data (list_id={list_id})...")
    current_resp = fetch_ranking_data(list_id)
    if not current_resp or current_resp.get("code") != 0 or not current_resp.get("data"):
        print(f"✗ Failed to fetch current data (list_id={list_id})")
        return
    current_data = current_resp["data"]
    print(f"✓ Current data: {len(current_data)} chars")

    print(f"\n⏳ Fetching previous data (list_id={prev_list_id})...")
    prev_resp = fetch_ranking_data(prev_list_id)
    if not prev_resp or prev_resp.get("code") != 0 or not prev_resp.get("data"):
        print(f"✗ Failed to fetch previous data (list_id={prev_list_id})")
        return
    previous_data = prev_resp["data"]
    print(f"✓ Previous data: {len(previous_data)} chars")

    print("\n⏳ Running Bedrock analysis...")
    analysis = analyze_with_bedrock(current_data, previous_data)

    if analysis:
        print(f"\n✓ Analysis result ({len(analysis)} chars):\n")
        print("-" * 60)
        print(analysis)
        print("-" * 60)
    else:
        print("✗ Analysis failed or returned empty")


def test_full_lambda():
    """Test the full Lambda handler (optional)."""
    print("\n" + "=" * 60)
    print("Test 4: Full Lambda Handler (Optional)")
    print("=" * 60)
    print("\nNote: This will try to access DynamoDB and post to Discord.")
    print("Make sure you have:")
    print("  1. AWS credentials configured (aws configure)")
    print("  2. DynamoDB table created")
    print("  3. Valid Discord webhook URL set")
    
    proceed = input("\nProceed with full Lambda test? (y/n): ")
    if proceed.lower() != "y":
        print("Skipped.")
        return
    
    print("\nRunning Lambda handler...")
    try:
        result = lambda_handler({}, None)
        print(f"\n✓ Lambda executed successfully!")
        print(f"  Status Code: {result['statusCode']}")
        print(f"  Response: {result['body']}")
    except Exception as e:
        print(f"\n✗ Lambda execution failed: {e}")
        print("  This is expected if DynamoDB is not accessible locally.")


def main():
    """Run all tests."""
    print("\n🧪 XZY Ranking Function - Local Test\n")
    
    # Check if webhook URL is set
    webhook_url = os.environ.get("XZY_WEBHOOK_URL")
    if webhook_url == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        print("⚠️  Warning: Discord webhook URL not set!")
        print("   Edit this file and replace 'YOUR_DISCORD_WEBHOOK_URL_HERE' with your actual webhook URL")
        print("   (Webhook posting will be skipped in tests)\n")
    
    # Test 1: Fetch data
    data = test_fetch_data()
    
    if not data:
        print("\n❌ Cannot proceed with other tests without data")
        return
    
    # Test 2: Format message (old text-based format)
    test_format_message(data)
    
    # Test 3: Generate images (new image-based format)
    test_generate_images(data)

    # Test 4: Bedrock analysis (current vs previous list_id from API)
    if data.get("data"):
        list_id = 106  # Adjust to the list_id used in test_fetch_data
        test_bedrock_analysis(list_id)

    # Test 5: Full Lambda (optional)
    test_full_lambda()
    
    print("\n" + "=" * 60)
    print("✅ Testing completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
