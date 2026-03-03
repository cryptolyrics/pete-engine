#!/usr/bin/env python3
"""
Draftstars Lineup Optimizer FINAL
"""
import csv

players = []
with open('/Users/jjbot/.openclaw/workspace/logs/Pete/draftstars_2026-02-25.csv', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        players.append(row)

available = [p for p in players if 'OUT' not in p['Playing Status'].upper()]

for p in available:
    p['salary'] = int(p['Salary'])
    p['fppg'] = float(p['FPPG'])
    p['value'] = p['fppg'] / p['salary'] * 1000

pos_players = {'PG': [], 'SG': [], 'SF': [], 'PF': [], 'C': []}
for p in available:
    for pos in p['Position'].split('/'):
        pos = pos.strip()
        if pos in pos_players:
            pos_players[pos].append(p)

for pos in pos_players:
    pos_players[pos].sort(key=lambda x: -x['fppg'])

SALARY_CAP = 100000

best_lineup = None
best_fppg = 0

# More flexible iteration
for pg1 in pos_players['PG'][:12]:
    for pg2 in pos_players['PG'][4:14]:
        for sg1 in pos_players['SG'][:12]:
            for sg2 in pos_players['SG'][4:14]:
                for sf1 in pos_players['SF'][:12]:
                    for sf2 in pos_players['SF'][4:14]:
                        base = pg1['salary']+pg2['salary']+sg1['salary']+sg2['salary']+sf1['salary']+sf2['salary']
                        if base > 85000:
                            continue
                        remaining = SALARY_CAP - base
                        
                        for pf1 in pos_players['PF'][:10]:
                            for pf2 in pos_players['PF'][3:12]:
                                pf_total = pf1['salary'] + pf2['salary']
                                if base + pf_total > 95000:
                                    continue
                                rem_c = remaining - pf_total
                                
                                for c in pos_players['C'][:10]:
                                    if c['salary'] > rem_c:
                                        continue
                                    
                                    total_fppg = (pg1['fppg'] + pg2['fppg'] + sg1['fppg'] + sg2['fppg'] + 
                                                 sf1['fppg'] + sf2['fppg'] + pf1['fppg'] + pf2['fppg'] + c['fppg'])
                                    
                                    if total_fppg > best_fppg:
                                        best_fppg = total_fppg
                                        best_lineup = [pg1, pg2, sg1, sg2, sf1, sf2, pf1, pf2, c]

if not best_lineup:
    # Fallback: greedy with better constraints
    print("Using fallback greedy...")
    lineup = []
    total = 0
    used = set()
    slots = [('PG',2), ('SG',2), ('SF',2), ('PF',2), ('C',1)]
    
    for pos, cnt in slots:
        for _ in range(cnt):
            for p in pos_players[pos]:
                key = p['Name']+p['Team']
                if key in used:
                    continue
                if total + p['salary'] <= 100000:
                    lineup.append(p)
                    used.add(key)
                    total += p['salary']
                    break
else:
    print("=== OPTIMAL LINEUP ===\n")
    pos_map = ['PG', 'PG', 'SG', 'SG', 'SF', 'SF', 'PF', 'PF', 'C']
    total_salary = 0
    for i, p in enumerate(best_lineup):
        print(f"{pos_map[i]}: {p['Name']:20} ${p['salary']:>5}  {p['fppg']:>5.1f} fppg")
        total_salary += p['salary']
    
    print(f"\n=== SUMMARY ===")
    print(f"Salary: ${total_salary:,} / $100,000")
    print(f"Remaining: ${SALARY_CAP - total_salary:,}")
    print(f"Projected: {best_fppg:.1f} fppg")
