from xtquant import xtdata
import time

def main():
    print("⏳ Starting sector data download...")
    xtdata.download_sector_data()
    print("✅ Sector data download command triggered. Waiting for synchronization...")
    
    # Wait a bit for the data to be processed and available
    time.sleep(5)
    
    sectors = xtdata.get_sector_list()
    print(f"Total sectors found after download: {len(sectors)}")
    
    # Search for industry or Shenwan related sectors
    keywords = ['行业', '申万', '板块', 'SW', '一级']
    matches = []
    for s in sectors:
        if any(kw in s for kw in keywords):
            matches.append(s)
            
    print(f"Matches found: {len(matches)}")
    for m in matches:
        print(m)
        
    if not matches:
        print("First 50 sectors for debugging:")
        for s in sectors[:50]:
            print(s)

if __name__ == "__main__":
    main()
