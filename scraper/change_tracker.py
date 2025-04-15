import pandas as pd

def track_changes(file_old, file_new):
    df_old = pd.read_csv(file_old)
    df_new = pd.read_csv(file_new)
    
    old_set = set(df_old["url"])
    new_set = set(df_new["url"])
    
    added = new_set - old_set
    removed = old_set - new_set

    added_df = df_new[df_new["url"].isin(added)]
    removed_df = df_old[df_old["url"].isin(removed)]

    return added_df, removed_df

