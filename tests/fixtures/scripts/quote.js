// A clean replay: reaches a quote, writes both status channels, exits 0.
// Also the negative control for the static checks -- it trips none of them.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

async function main() {
  const answers = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const config = readConfig();

  const behavior = new HumanBehavior(page);
  const { release } = await acquireCarrierLoginLock();
  await login(config.LOGIN_EMAIL, config.LOGIN_PASSWORD);
  await pickYesNo(answers.answers.hasPriorClaims);
  release();

  return {
    outcome: 'quote',
    reachedQuote: true,
    stoppedReason: null,
    premium: 3036,
    premiumDisplay: '$3,036/yr',
    quoteNumber: 'Q-88213',
    quoteSaved: true,
    quoteUrl: 'https://portal.example.com/quote/88213',
    bindUrl: 'https://portal.example.com/bind/88213',
    bindControlLabel: 'Request to Bind',
    documents: []
  };
}

function readConfig() {
  const i = process.argv.indexOf('--config');
  return i === -1 ? {} : JSON.parse(fs.readFileSync(process.argv[i + 1], 'utf8'));
}

function report(status) {
  fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
  console.log('RRSTATUS ' + JSON.stringify(status));
  process.exit(status.outcome === 'quote' || status.outcome === 'appetite-decline' ? 0 : 1);
}

// The fixture never opens a browser; these stubs give the script the call
// shapes the static checks look for without a Playwright dependency.
function HumanBehavior() {}
function acquireCarrierLoginLock() { return Promise.resolve({ release: function () {} }); }
function login() { return Promise.resolve(); }
function pickYesNo() { return Promise.resolve(); }
const page = {};

main().then(report).catch(function (err) {
  report({ reachedQuote: false, outcome: 'stuck', stoppedReason: err.message });
});
