import requests
import pandas as pd
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
from datetime import datetime, timedelta

def run_pete_dfs_engine(daily_csv_path):
    print(f"🚀 PETE DFS ENGINE: Processing {daily_csv_path}...")

    # 1. SCRAPE LATEST BOX SCORES (No NBA_api needed)
    print("🕷️ Scraping ESPN for historical usage gaps...")
    all_logs = []
    for i in range(10):  # Last 10 days
        date_str = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
        url = f"http://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date_str}"
        try:
            r = requests.get(url).json()
            for event in r.get('events', []):
                g_id = event['id']
                box = requests.get(f"http://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={g_id}").json()
                for team in box['boxscore']['players']:
                    for athlete in team['statistics'][0]['athletes']:
                        s = athlete['stats']
                        if len(s) < 13:
                            continue
                        # Draftstars Scoring Formula
                        fpts = (float(s[12])*1)+(float(s[6])*1.25)+(float(s[7])*1.5)+(float(s[8])*2)+(float(s[9])*2)-(float(s[10])*0.5)
                        all_logs.append({'Name': athlete['athlete']['displayName'], 'FP': fpts})
        except:
            continue

    history_df = pd.DataFrame(all_logs)
    variance_map = history_df.groupby('Name')['FP'].std().to_dict()

    # 2. LOAD & CLEAN DAILY DATA
    df = pd.read_csv(daily_csv_path)
    df['Salary'] = pd.to_numeric(df['Salary'], errors='coerce')
    df['FPPG'] = pd.to_numeric(df['FPPG'], errors='coerce')
    df['Form'] = pd.to_numeric(df['Form'], errors='coerce')

    # Apply "Kalkbrenner" Safety Filter (Scrub OUT/Questionable)
    df = df[~df['Playing Status'].str.contains('OUT|QUESTIONABLE|PROBABLE', na=False, case=False)].copy()
    df = df.dropna(subset=['Salary', 'Form']).reset_index(drop=True)

    # 3. MILP OPTIMIZATION (Strict 2-2-2-2-1 Format)
    n = len(df)
    c = -df['Form'].values

    A_eq = [np.ones(n)]  # Total 9 players
    b_eq = [9]

    # Positional Requirements
    for pos, count in {'PG':2, 'SG':2, 'SF':2, 'PF':2, 'C':1}.items():
        A_eq.append((df['Position'] == pos).values.astype(float))
        b_eq.append(count)

    A_ub = [df['Salary'].values]  # Salary Cap
    b_ub = [100000]

    # Handle Multi-Position Duplicates
    for name in df['Name'].unique():
        idx = (df['Name'] == name).values
        if sum(idx) > 1:
            A_ub.append(idx.astype(float))
            b_ub.append(1)

    res = milp(c=c, constraints=LinearConstraint(np.vstack([A_eq, A_ub]), np.concatenate([b_eq, np.full(len(b_ub), -np.inf)]), np.concatenate([b_eq, b_ub])), integrality=np.ones(n), bounds=Bounds(0, 1))

    if res.success:
        lineup = df.iloc[np.where(np.round(res.x) == 1)[0]]
        print("\n--- FINAL OPTIMIZED LINEUP ---")
        print(lineup[['Position', 'Name', 'Team', 'Salary', 'Form']].sort_values('Position').to_string(index=False))
        print(f"\nTotal Salary: ${lineup['Salary'].sum():,.0f} | Projected Form: {lineup['Form'].sum():.2f}")
    else:
        print("Optimization failed.")

# RUN COMMAND:
# run_pete_dfs_engine('/Users/jjbot/.openclaw/workspace/logs/Pete/draftstars_2026-02-25.csv')
