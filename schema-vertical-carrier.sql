-- Verticals (business categories with embeddings)
CREATE TABLE IF NOT EXISTS verticals (
  id SERIAL PRIMARY KEY,
  vertical_short TEXT NOT NULL UNIQUE,
  embedding_short vector(1536),
  vertical_long TEXT,
  embedding_long vector(1536),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_verticals_embedding_short ON verticals USING ivfflat (embedding_short vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_verticals_embedding_long ON verticals USING ivfflat (embedding_long vector_cosine_ops) WITH (lists = 100);

-- Carriers (insurance companies)
CREATE TABLE IF NOT EXISTS carriers (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active', -- 'active', 'inactive', 'paused'
  is_admitted BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Carrier credentials (login info for web crawling)
CREATE TABLE IF NOT EXISTS carrier_creds (
  id SERIAL PRIMARY KEY,
  carrier_id INTEGER NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
  username TEXT NOT NULL,
  password TEXT NOT NULL,
  login_url TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_carrier_creds_carrier_id ON carrier_creds(carrier_id);

-- Carrier product combinations (carrier + insurance type + business type)
CREATE TABLE IF NOT EXISTS carrier_combo (
  id SERIAL PRIMARY KEY,
  carrier_id INTEGER NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
  insurance_type TEXT NOT NULL, -- e.g., 'general_liability', 'workers_comp', 'property'
  business_type TEXT NOT NULL, -- e.g., 'retail', 'manufacturing', 'service'
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(carrier_id, insurance_type, business_type)
);

CREATE INDEX idx_carrier_combo_carrier_id ON carrier_combo(carrier_id);
CREATE INDEX idx_carrier_combo_insurance_type ON carrier_combo(insurance_type);
CREATE INDEX idx_carrier_combo_business_type ON carrier_combo(business_type);

-- Carrier appetite (which verticals does each carrier underwrite)
CREATE TABLE IF NOT EXISTS appetites (
  id SERIAL PRIMARY KEY,
  carrier_id INTEGER NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
  insurance_type TEXT NOT NULL,
  vertical_id INTEGER NOT NULL REFERENCES verticals(id) ON DELETE CASCADE,
  min_premium DECIMAL(10, 2),
  max_premium DECIMAL(10, 2),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(carrier_id, insurance_type, vertical_id)
);

CREATE INDEX idx_appetites_carrier_id ON appetites(carrier_id);
CREATE INDEX idx_appetites_vertical_id ON appetites(vertical_id);
CREATE INDEX idx_appetites_insurance_type ON appetites(insurance_type);

-- Question embeddings (underwriting questions with embeddings)
CREATE TABLE IF NOT EXISTS question_embeddings (
  id SERIAL PRIMARY KEY,
  text_short TEXT NOT NULL,
  text_long TEXT,
  embedding_short vector(1536),
  embedding_long vector(1536),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_question_embeddings_embedding_short ON question_embeddings USING ivfflat (embedding_short vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_question_embeddings_embedding_long ON question_embeddings USING ivfflat (embedding_long vector_cosine_ops) WITH (lists = 100);

-- Carrier-specific questions (which questions does each carrier_combo ask)
CREATE TABLE IF NOT EXISTS carrier_questions (
  id SERIAL PRIMARY KEY,
  carrier_combo_id INTEGER NOT NULL REFERENCES carrier_combo(id) ON DELETE CASCADE,
  question_id INTEGER NOT NULL REFERENCES question_embeddings(id) ON DELETE CASCADE,
  order_index INTEGER,
  is_required BOOLEAN DEFAULT true,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(carrier_combo_id, question_id)
);

CREATE INDEX idx_carrier_questions_carrier_combo_id ON carrier_questions(carrier_combo_id);
CREATE INDEX idx_carrier_questions_question_id ON carrier_questions(question_id);

-- Crawl artifacts (metadata + questions + automation script for web crawling)
CREATE TABLE IF NOT EXISTS crawl_artifacts (
  id SERIAL PRIMARY KEY,
  carrier_combo_id INTEGER NOT NULL REFERENCES carrier_combo(id) ON DELETE CASCADE,
  metadata JSONB, -- e.g., { "url_pattern": "...", "last_crawled": "...", "status": "..." }
  questions JSONB, -- e.g., array of questions for this carrier combo
  script TEXT, -- Playwright script for automating the crawl
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_crawl_artifacts_carrier_combo_id ON crawl_artifacts(carrier_combo_id);

-- Quote artifacts (quote documents + bind URLs)
CREATE TABLE IF NOT EXISTS quote_artifacts (
  id SERIAL PRIMARY KEY,
  carrier_combo_id INTEGER NOT NULL REFERENCES carrier_combo(id) ON DELETE CASCADE,
  quote_document JSONB, -- e.g., { "coverage": "...", "premium": "...", "terms": "..." }
  bind_url TEXT, -- URL to bind/purchase the quote
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_quote_artifacts_carrier_combo_id ON quote_artifacts(carrier_combo_id);
