from pathlib import Path
from typing import List

import pandas as pd


class EnrichmentManager:
    def __init__(self, log_level:int):
        self.enrichment_pools = []
        self.enrichment_count = 0
        self._enrichment_keys = set()
        self.log_level = log_level

    def read_file(self, enrichment_path:Path, enrich_cols: List[str]):
        """
        Read enrichment file and add enrichment data.
        """
        try:
            enrich_df = pd.read_csv(enrichment_path, dtype={"osm_id": str})
        except Exception as e:
            raise ValueError(f"❌ Failed to read enrichment file: {enrichment_path}\n{e}")

        if "osm_id" not in enrich_df.columns:
            raise ValueError(f"❌ Invalid enrichment file: '{enrichment_path}'  - missing 'osm_id' column")

        missing_cols = [col for col in enrich_cols if col not in enrich_df.columns]
        if missing_cols:
            found_cols = sorted(enrich_df.columns.tolist())
            raise ValueError(
                f"❌ Enrichment file missing expected columns: {missing_cols}\n"
                f"   ➤ Columns found: {found_cols}"
            )

            enrich_df["osm_id"] = enrich_df["osm_id"].str.strip()

        dup_ids = enrich_df["osm_id"][enrich_df["osm_id"].duplicated(keep=False)]
        if not dup_ids.empty:
            sample_id = dup_ids.iloc[0]
            print(
                f"⚠️ Found {len(dup_ids)} duplicate osm_id entries. Keeping last. Sample: "
                f"{sample_id}"
                )
            enrich_df = enrich_df.drop_duplicates(subset="osm_id", keep="last")

        req_cols = set(enrich_cols) | {"osm_id"}
        available = set(enrich_df.columns)
        keep_cols = list(req_cols & available)
        enrich_df = enrich_df[keep_cols].set_index("osm_id")

        enrichment_data = {}
        for osm_id, row in enrich_df.iterrows():
            filtered = {col: val for col, val in row.items() if pd.notna(val)}
            if filtered:
                enrichment_data[str(osm_id)] = filtered

        self._enrichment_keys.update(enrich_df.columns.difference(["osm_id"]))
        self.enrichment_pools.append(enrichment_data)
        if self.log_level > 1:
            print(f"✅ Read enrichment file '{enrichment_path}':\n  {len(enrichment_data)} node entries. Columns: {enrich_cols}")

    def apply_to_node(self, osm_id: str, row: dict, log_dbg) -> None:
        # Apply any enrichment data we have for this osm_id
        for source in self.enrichment_pools:
            extra = source.get(osm_id)
            if extra:
                self.enrichment_count += 1
                row.update(extra)
                updates = [f"{k} = {v}" for k, v in extra.items()]
                if updates:
                    log_dbg(osm_id, "🔁 Enrichment: " + "; ".join(updates))

    def keys(self):
        return list(self._enrichment_keys)
