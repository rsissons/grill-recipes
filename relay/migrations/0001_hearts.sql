CREATE TABLE IF NOT EXISTS hearts (
  recipe TEXT NOT NULL,
  device TEXT NOT NULL,
  created INTEGER NOT NULL,
  PRIMARY KEY (recipe, device)
);
CREATE INDEX IF NOT EXISTS hearts_recipe ON hearts (recipe);
