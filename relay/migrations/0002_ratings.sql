CREATE TABLE IF NOT EXISTS ratings (
  recipe TEXT NOT NULL,
  device TEXT NOT NULL,
  stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
  updated INTEGER NOT NULL,
  PRIMARY KEY (recipe, device)
);
CREATE INDEX IF NOT EXISTS ratings_recipe ON ratings (recipe);
