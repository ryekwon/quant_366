from xtquant import xtdata

def explore():
    sectors = xtdata.get_sector_list()
    print(f"Total sectors found: {len(sectors)}")
    
    # Search for industry or Shenwan related sectors
    keywords = ['行业', '申万', '板块', 'SW', '一级']
    matches = []
    for s in sectors:
        if any(kw in s for kw in keywords):
            matches.append(s)
            
    print(f"Matches found: {len(matches)}")
    for m in matches[:50]:
        print(m)
        
    # If no matches, print first 100 to see format
    if not matches:
        print("First 100 sectors:")
        for s in sectors[:100]:
            print(s)

if __name__ == "__main__":
    explore()
