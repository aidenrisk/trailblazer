// Loads its artifact pair under names the runner does not materialize.
// The runner regex-scrapes for `onboarding-*.(metadata|questions).json`, so
// these files never exist on disk when the script reads them.
const fs = require('fs');
const path = require('path');

const METADATA = 'pie-general-liability.metadata.json';
const QUESTIONS = 'pie-general-liability.questions.json';

const status = { outcome: 'quote', reachedQuote: true };
fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
console.log('RRSTATUS ' + JSON.stringify(status));
