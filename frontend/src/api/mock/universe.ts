import type {
  CompanySummary,
  DataQuality,
  FactorFamily,
  FactorScores,
  KpiProfile,
  MarketCapBucket,
  MoatRating,
  RegionCode,
  ScreenerRow,
} from '../types'
import { COUNTRIES, FX_TO_USD } from '@/lib/regions'
import { DEFAULT_WEIGHTS, FACTOR_FAMILIES, KPI_SETS, SUBSECTOR_INDEX } from './taxonomy'
import { Rng, hashString, round } from './rng'

/* ── exchanges ── */

interface ExchangeDef {
  name: string
  suffix: string
  usd?: boolean
}
const EX: Record<string, ExchangeDef> = {
  NYSE: { name: 'NYSE', suffix: '', usd: true },
  NASDAQ: { name: 'NASDAQ', suffix: '', usd: true },
  XETRA: { name: 'XETRA', suffix: '.DE' },
  EPA: { name: 'Euronext Paris', suffix: '.PA' },
  AMS: { name: 'Euronext Amsterdam', suffix: '.AS' },
  BME: { name: 'BME Madrid', suffix: '.MC' },
  MIL: { name: 'Borsa Italiana', suffix: '.MI' },
  ELI: { name: 'Euronext Lisbon', suffix: '.LS' },
  EBR: { name: 'Euronext Brussels', suffix: '.BR' },
  SIX: { name: 'SIX Swiss', suffix: '.SW' },
  CPH: { name: 'Nasdaq Copenhagen', suffix: '.CO' },
  STO: { name: 'Nasdaq Stockholm', suffix: '.ST' },
  OSL: { name: 'Oslo Børs', suffix: '.OL' },
  HEL: { name: 'Nasdaq Helsinki', suffix: '.HE' },
  LSE: { name: 'LSE', suffix: '.L' },
  TSE: { name: 'Tokyo SE', suffix: '.T' },
  KRX: { name: 'KRX', suffix: '.KS' },
  HKEX: { name: 'HKEX', suffix: '.HK' },
  SSE: { name: 'Shanghai SE', suffix: '.SS' },
  SZSE: { name: 'Shenzhen SE', suffix: '.SZ' },
  B3: { name: 'B3', suffix: '.SA' },
  BMV: { name: 'BMV', suffix: '.MX' },
}

/* ── real-name seed universe: ticker|name|exchange|country|subsector|mcap($bn) ── */

const SEED = `
AAPL|Apple Inc.|NASDAQ|US|hardware|3400
MSFT|Microsoft Corp.|NASDAQ|US|software|3200
NVDA|NVIDIA Corp.|NASDAQ|US|semis|3000
GOOGL|Alphabet Inc.|NASDAQ|US|interactive|2100
AMZN|Amazon.com Inc.|NASDAQ|US|retail|1900
META|Meta Platforms Inc.|NASDAQ|US|interactive|1400
AVGO|Broadcom Inc.|NASDAQ|US|semis|800
TSLA|Tesla Inc.|NASDAQ|US|autos|750
BRK.B|Berkshire Hathaway Inc.|NYSE|US|insurance|950
JPM|JPMorgan Chase & Co.|NYSE|US|banks|620
V|Visa Inc.|NYSE|US|payments|560
MA|Mastercard Inc.|NYSE|US|payments|450
UNH|UnitedHealth Group Inc.|NYSE|US|hc_services|480
XOM|Exxon Mobil Corp.|NYSE|US|integrated|470
LLY|Eli Lilly and Co.|NYSE|US|pharma|750
JNJ|Johnson & Johnson|NYSE|US|pharma|380
PG|Procter & Gamble Co.|NYSE|US|household|390
HD|Home Depot Inc.|NYSE|US|retail|380
COST|Costco Wholesale Corp.|NASDAQ|US|food_retail|400
WMT|Walmart Inc.|NYSE|US|food_retail|650
BAC|Bank of America Corp.|NYSE|US|banks|320
ORCL|Oracle Corp.|NYSE|US|software|450
CRM|Salesforce Inc.|NYSE|US|software|260
ADBE|Adobe Inc.|NASDAQ|US|software|220
AMD|Advanced Micro Devices Inc.|NASDAQ|US|semis|260
NFLX|Netflix Inc.|NASDAQ|US|entertainment|300
KO|Coca-Cola Co.|NYSE|US|food_bev|300
PEP|PepsiCo Inc.|NASDAQ|US|food_bev|230
CVX|Chevron Corp.|NYSE|US|integrated|280
MRK|Merck & Co. Inc.|NYSE|US|pharma|250
ABBV|AbbVie Inc.|NYSE|US|pharma|320
TMO|Thermo Fisher Scientific Inc.|NYSE|US|medtech|210
ISRG|Intuitive Surgical Inc.|NASDAQ|US|medtech|180
CAT|Caterpillar Inc.|NYSE|US|machinery|180
GE|GE Aerospace|NYSE|US|aerospace|200
BA|Boeing Co.|NYSE|US|aerospace|130
RTX|RTX Corp.|NYSE|US|aerospace|160
LMT|Lockheed Martin Corp.|NYSE|US|aerospace|110
HON|Honeywell International Inc.|NASDAQ|US|machinery|135
DE|Deere & Co.|NYSE|US|machinery|120
UNP|Union Pacific Corp.|NYSE|US|transport|140
UPS|United Parcel Service Inc.|NYSE|US|transport|110
GS|Goldman Sachs Group Inc.|NYSE|US|capital_markets|170
MS|Morgan Stanley|NYSE|US|capital_markets|200
SCHW|Charles Schwab Corp.|NYSE|US|capital_markets|140
WFC|Wells Fargo & Co.|NYSE|US|banks|240
C|Citigroup Inc.|NYSE|US|banks|140
PGR|Progressive Corp.|NYSE|US|insurance|150
CB|Chubb Ltd.|NYSE|US|insurance|115
NOW|ServiceNow Inc.|NYSE|US|software|190
INTU|Intuit Inc.|NASDAQ|US|software|180
PANW|Palo Alto Networks Inc.|NASDAQ|US|software|120
CRWD|CrowdStrike Holdings Inc.|NASDAQ|US|software|90
SNOW|Snowflake Inc.|NYSE|US|software|60
QCOM|Qualcomm Inc.|NASDAQ|US|semis|180
TXN|Texas Instruments Inc.|NASDAQ|US|semis|170
AMAT|Applied Materials Inc.|NASDAQ|US|semis|150
MU|Micron Technology Inc.|NASDAQ|US|semis|110
ACN|Accenture plc|NYSE|US|it_services|210
IBM|International Business Machines|NYSE|US|it_services|200
NEE|NextEra Energy Inc.|NYSE|US|electric_util|150
DUK|Duke Energy Corp.|NYSE|US|electric_util|85
SO|Southern Co.|NYSE|US|electric_util|95
PLD|Prologis Inc.|NYSE|US|reits|105
AMT|American Tower Corp.|NYSE|US|reits|95
EQIX|Equinix Inc.|NASDAQ|US|reits|80
O|Realty Income Corp.|NYSE|US|reits|50
SPG|Simon Property Group Inc.|NYSE|US|reits|55
COP|ConocoPhillips|NYSE|US|e_p|125
EOG|EOG Resources Inc.|NYSE|US|e_p|70
SLB|SLB N.V.|NYSE|US|oil_services|60
LIN|Linde plc|NYSE|US|chemicals|210
FCX|Freeport-McMoRan Inc.|NYSE|US|mining|65
NKE|Nike Inc.|NYSE|US|luxury|110
MCD|McDonald's Corp.|NYSE|US|leisure|210
SBUX|Starbucks Corp.|NASDAQ|US|leisure|100
T|AT&T Inc.|NYSE|US|telecom|160
VZ|Verizon Communications Inc.|NYSE|US|telecom|175
DIS|Walt Disney Co.|NYSE|US|entertainment|200
PFE|Pfizer Inc.|NYSE|US|pharma|160
AMGN|Amgen Inc.|NASDAQ|US|biotech|160
VRTX|Vertex Pharmaceuticals Inc.|NASDAQ|US|biotech|120
REGN|Regeneron Pharmaceuticals Inc.|NASDAQ|US|biotech|80
F|Ford Motor Co.|NYSE|US|autos|45
GM|General Motors Co.|NYSE|US|autos|55
MO|Altria Group Inc.|NYSE|US|tobacco|90
PM|Philip Morris International|NYSE|US|tobacco|190
ASML|ASML Holding N.V.|AMS|NL|semis|280
SAP|SAP SE|XETRA|DE|software|300
MC|LVMH Moët Hennessy Louis Vuitton|EPA|FR|luxury|330
OR|L'Oréal SA|EPA|FR|household|220
RMS|Hermès International|EPA|FR|luxury|250
NOVO-B|Novo Nordisk A/S|CPH|DK|pharma|350
NESN|Nestlé SA|SIX|CH|food_bev|260
ROG|Roche Holding AG|SIX|CH|pharma|230
NOVN|Novartis AG|SIX|CH|pharma|220
SIE|Siemens AG|XETRA|DE|machinery|160
ALV|Allianz SE|XETRA|DE|insurance|120
DTE|Deutsche Telekom AG|XETRA|DE|telecom|150
TTE|TotalEnergies SE|EPA|FR|integrated|140
SAN|Banco Santander SA|BME|ES|banks|85
BBVA|Banco Bilbao Vizcaya Argentaria|BME|ES|banks|70
IBE|Iberdrola SA|BME|ES|electric_util|95
ITX|Industria de Diseño Textil SA|BME|ES|retail|170
ENEL|Enel SpA|MIL|IT|electric_util|75
ISP|Intesa Sanpaolo SpA|MIL|IT|banks|75
UCG|UniCredit SpA|MIL|IT|banks|70
RACE|Ferrari N.V.|MIL|IT|autos|85
BNP|BNP Paribas SA|EPA|FR|banks|80
AIR|Airbus SE|EPA|FR|aerospace|140
SU|Schneider Electric SE|EPA|FR|electrical|140
SAF|Safran SA|EPA|FR|aerospace|110
DSY|Dassault Systèmes SE|EPA|FR|software|50
CAP|Capgemini SE|EPA|FR|it_services|30
URW|Unibail-Rodamco-Westfield|EPA|FR|reits|11
MBG|Mercedes-Benz Group AG|XETRA|DE|autos|65
BMW|Bayerische Motoren Werke AG|XETRA|DE|autos|55
VOW3|Volkswagen AG|XETRA|DE|autos|55
BAS|BASF SE|XETRA|DE|chemicals|45
VNA|Vonovia SE|XETRA|DE|reits|25
ADYEN|Adyen N.V.|AMS|NL|payments|50
INGA|ING Groep N.V.|AMS|NL|banks|60
PRX|Prosus N.V.|AMS|NL|interactive|100
EDP|EDP – Energias de Portugal SA|ELI|PT|electric_util|15
EDPR|EDP Renováveis SA|ELI|PT|renewables|12
GALP|Galp Energia SGPS SA|ELI|PT|integrated|13
JMT|Jerónimo Martins SGPS SA|ELI|PT|food_retail|14
BCP|Banco Comercial Português SA|ELI|PT|banks|10
ABI|Anheuser-Busch InBev SA/NV|EBR|BE|food_bev|110
NOKIA|Nokia Oyj|HEL|FI|hardware|22
ERIC-B|Telefonaktiebolaget LM Ericsson|STO|SE|hardware|25
VOLV-B|AB Volvo|STO|SE|machinery|55
ATCO-A|Atlas Copco AB|STO|SE|machinery|80
EQNR|Equinor ASA|OSL|NO|integrated|70
SHEL|Shell plc|LSE|GB|integrated|210
AZN|AstraZeneca plc|LSE|GB|pharma|230
HSBA|HSBC Holdings plc|LSE|GB|banks|170
ULVR|Unilever plc|LSE|GB|household|150
BP.|BP plc|LSE|GB|integrated|85
GSK|GSK plc|LSE|GB|pharma|75
RIO|Rio Tinto plc|LSE|GB|mining|105
REL|RELX plc|LSE|GB|it_services|90
DGE|Diageo plc|LSE|GB|food_bev|65
BATS|British American Tobacco plc|LSE|GB|tobacco|85
LSEG|London Stock Exchange Group plc|LSE|GB|capital_markets|70
BARC|Barclays plc|LSE|GB|banks|45
LLOY|Lloyds Banking Group plc|LSE|GB|banks|45
NWG|NatWest Group plc|LSE|GB|banks|40
RR.|Rolls-Royce Holdings plc|LSE|GB|aerospace|60
BA.|BAE Systems plc|LSE|GB|aerospace|50
NG.|National Grid plc|LSE|GB|electric_util|60
VOD|Vodafone Group plc|LSE|GB|telecom|25
TSCO|Tesco plc|LSE|GB|food_retail|32
LGEN|Legal & General Group plc|LSE|GB|insurance|18
LAND|Land Securities Group plc|LSE|GB|reits|6
SGRO|Segro plc|LSE|GB|reits|12
GLEN|Glencore plc|LSE|GB|mining|60
AAL|Anglo American plc|LSE|GB|mining|40
EXPN|Experian plc|LSE|GB|it_services|45
ARM|Arm Holdings plc|NASDAQ|GB|semis|150
7203|Toyota Motor Corp.|TSE|JP|autos|280
6758|Sony Group Corp.|TSE|JP|hardware|140
6861|Keyence Corp.|TSE|JP|electrical|110
8306|Mitsubishi UFJ Financial Group|TSE|JP|banks|140
9984|SoftBank Group Corp.|TSE|JP|telecom|90
6501|Hitachi Ltd.|TSE|JP|electrical|110
8035|Tokyo Electron Ltd.|TSE|JP|semis|85
9432|Nippon Telegraph & Telephone|TSE|JP|telecom|90
4063|Shin-Etsu Chemical Co.|TSE|JP|chemicals|80
6098|Recruit Holdings Co.|TSE|JP|it_services|95
7974|Nintendo Co.|TSE|JP|entertainment|75
8316|Sumitomo Mitsui Financial Group|TSE|JP|banks|95
4502|Takeda Pharmaceutical Co.|TSE|JP|pharma|45
4568|Daiichi Sankyo Co.|TSE|JP|pharma|60
6367|Daikin Industries Ltd.|TSE|JP|machinery|40
7267|Honda Motor Co.|TSE|JP|autos|50
6902|Denso Corp.|TSE|JP|autos|40
8058|Mitsubishi Corp.|TSE|JP|trading|75
8001|ITOCHU Corp.|TSE|JP|trading|75
9983|Fast Retailing Co.|TSE|JP|retail|100
8766|Tokio Marine Holdings|TSE|JP|insurance|75
6954|Fanuc Corp.|TSE|JP|machinery|30
6146|Disco Corp.|TSE|JP|semis|30
6273|SMC Corp.|TSE|JP|machinery|30
8801|Mitsui Fudosan Co.|TSE|JP|re_dev|25
4661|Oriental Land Co.|TSE|JP|leisure|40
2914|Japan Tobacco Inc.|TSE|JP|tobacco|50
005930|Samsung Electronics Co.|KRX|KR|semis|330
000660|SK hynix Inc.|KRX|KR|semis|120
373220|LG Energy Solution Ltd.|KRX|KR|electrical|75
207940|Samsung Biologics Co.|KRX|KR|biotech|50
005380|Hyundai Motor Co.|KRX|KR|autos|40
000270|Kia Corp.|KRX|KR|autos|30
035420|NAVER Corp.|KRX|KR|interactive|25
035720|Kakao Corp.|KRX|KR|interactive|15
051910|LG Chem Ltd.|KRX|KR|chemicals|18
105560|KB Financial Group Inc.|KRX|KR|banks|25
055550|Shinhan Financial Group Co.|KRX|KR|banks|20
005490|POSCO Holdings Inc.|KRX|KR|mining|20
068270|Celltrion Inc.|KRX|KR|biotech|30
012330|Hyundai Mobis Co.|KRX|KR|autos|17
017670|SK Telecom Co.|KRX|KR|telecom|9
0700|Tencent Holdings Ltd.|HKEX|CN|interactive|480
9988|Alibaba Group Holding Ltd.|HKEX|CN|retail|250
3690|Meituan|HKEX|CN|retail|110
1299|AIA Group Ltd.|HKEX|HK|insurance|85
0939|China Construction Bank Corp.|HKEX|CN|banks|200
1398|Industrial & Commercial Bank of China|HKEX|CN|banks|260
0941|China Mobile Ltd.|HKEX|CN|telecom|220
1211|BYD Co. Ltd.|HKEX|CN|autos|120
0388|Hong Kong Exchanges & Clearing|HKEX|HK|capital_markets|50
0883|CNOOC Ltd.|HKEX|CN|e_p|110
2318|Ping An Insurance (Group) Co.|HKEX|CN|insurance|120
9618|JD.com Inc.|HKEX|CN|retail|55
1810|Xiaomi Corp.|HKEX|CN|hardware|140
0016|Sun Hung Kai Properties Ltd.|HKEX|HK|re_dev|30
0001|CK Hutchison Holdings Ltd.|HKEX|HK|trading|20
0002|CLP Holdings Ltd.|HKEX|HK|electric_util|22
600519|Kweichow Moutai Co.|SSE|CN|food_bev|260
300750|Contemporary Amperex Technology|SZSE|CN|electrical|180
600036|China Merchants Bank Co.|SSE|CN|banks|130
000858|Wuliangye Yibin Co.|SZSE|CN|food_bev|75
601899|Zijin Mining Group Co.|SSE|CN|mining|70
688981|Semiconductor Manufacturing Intl.|SSE|CN|semis|70
600900|China Yangtze Power Co.|SSE|CN|electric_util|95
000333|Midea Group Co.|SZSE|CN|durables|75
PETR4|Petróleo Brasileiro SA – Petrobras|B3|BR|integrated|85
VALE3|Vale SA|B3|BR|mining|45
ITUB4|Itaú Unibanco Holding SA|B3|BR|banks|60
BBDC4|Banco Bradesco SA|B3|BR|banks|28
BBAS3|Banco do Brasil SA|B3|BR|banks|25
ABEV3|Ambev SA|B3|BR|food_bev|38
WEGE3|WEG SA|B3|BR|electrical|35
B3SA3|B3 SA – Brasil, Bolsa, Balcão|B3|BR|capital_markets|13
RENT3|Localiza Rent a Car SA|B3|BR|transport|8
RADL3|Raia Drogasil SA|B3|BR|food_retail|8
SUZB3|Suzano SA|B3|BR|chemicals|13
ELET3|Centrais Elétricas Brasileiras SA|B3|BR|electric_util|16
NU|Nu Holdings Ltd.|NYSE|BR|banks|65
MELI|MercadoLibre Inc.|NASDAQ|AR|retail|100
SQM|Sociedad Química y Minera de Chile|NYSE|CL|chemicals|12
WALMEX|Wal-Mart de México SAB de CV|BMV|MX|food_retail|55
AMXB|América Móvil SAB de CV|BMV|MX|telecom|55
GFNORTEO|Grupo Financiero Banorte|BMV|MX|banks|25
FEMSAUBD|Fomento Económico Mexicano|BMV|MX|food_bev|35
GMEXICOB|Grupo México SAB de CV|BMV|MX|mining|45
CEMEXCPO|Cemex SAB de CV|BMV|MX|construction_mat|10
ASURB|Grupo Aeroportuario del Sureste|BMV|MX|transport|9
GAPB|Grupo Aeroportuario del Pacífico|BMV|MX|transport|10
BIMBOA|Grupo Bimbo SAB de CV|BMV|MX|food_bev|15
`

/**
 * Rough recent prices (trading currency; LSE in GBP, not pence) for the real-name seed universe.
 * The mock price path is a random walk that ends at this level, so headline prices look plausible.
 */
const REAL_PRICES_RAW = `
AAPL 255|MSFT 515|NVDA 185|GOOGL 245|AMZN 225|META 720|AVGO 340|TSLA 430|BRK.B 495|JPM 305|V 345|MA 575|UNH 345|XOM 114|LLY 820
JNJ 190|PG 152|HD 405|COST 925|WMT 102|BAC 51|ORCL 290|CRM 245|ADBE 350|AMD 165|NFLX 1200|KO 68|PEP 145|CVX 155|MRK 85|ABBV 230
TMO 490|ISRG 450|CAT 480|GE 300|BA 215|RTX 165|LMT 495|HON 210|DE 465|UNP 230|UPS 85|GS 785|MS 158|SCHW 95|WFC 82|C 100|PGR 245
CB 280|NOW 920|INTU 670|PANW 205|CRWD 490|SNOW 225|QCOM 165|TXN 185|AMAT 205|MU 185|ACN 245|IBM 285|NEE 80|DUK 125|SO 95|PLD 115
AMT 190|EQIX 800|O 58|SPG 180|COP 92|EOG 112|SLB 35|LIN 470|FCX 42|NKE 70|MCD 300|SBUX 85|T 28|VZ 43|DIS 113|PFE 25|AMGN 290
VRTX 400|REGN 560|F 12|GM 60|MO 65|PM 160
ASML 860|SAP 230|MC 540|OR 375|RMS 2150|NOVO-B 330|NESN 80|ROG 270|NOVN 100|SIE 235|ALV 345|DTE 29|TTE 54|SAN 8.8|BBVA 16.5
IBE 16.2|ITX 46|ENEL 8.2|ISP 5.6|UCG 65|RACE 380|BNP 78|AIR 205|SU 235|SAF 300|DSY 28|CAP 125|URW 88|MBG 54|BMW 85|VOW3 95|BAS 43
VNA 26|ADYEN 1400|INGA 21|PRX 55|EDP 3.9|EDPR 10.5|GALP 16.5|JMT 21|BCP 0.72|ABI 52|NOKIA 4.3|ERIC-B 78|VOLV-B 280|ATCO-A 160|EQNR 250
SHEL 27|AZN 115|HSBA 10|ULVR 45|BP. 4.3|GSK 15|RIO 48|REL 36|DGE 18.5|BATS 40|LSEG 90|BARC 3.8|LLOY 0.82|NWG 5.4|RR. 11.5|BA. 19
NG. 10.5|VOD 0.85|TSCO 4.4|LGEN 2.4|LAND 5.9|SGRO 6.8|GLEN 3.4|AAL 27|EXPN 37|ARM 150
7203 2900|6758 4200|6861 55000|8306 2300|9984 18000|6501 4000|8035 26000|9432 155|4063 4600|6098 8500|7974 13000|8316 4000|4502 4300
4568 3600|6367 17500|7267 1600|6902 2000|8058 3400|8001 8500|9983 50000|8766 5800|6954 4400|6146 45000|6273 50000|8801 1550|4661 2900|2914 4600
005930 88000|000660 330000|373220 360000|207940 1050000|005380 220000|000270 105000|035420 230000|035720 60000|051910 300000
105560 115000|055550 58000|005490 330000|068270 175000|012330 300000|017670 52000
0700 620|9988 160|3690 105|1299 75|0939 7.8|1398 6.0|0941 87|1211 110|0388 420|0883 19.5|2318 55|9618 130|1810 52|0016 95|0001 52|0002 66
600519 1450|300750 380|600036 45|000858 125|601899 28|688981 120|600900 28|000333 76
PETR4 31|VALE3 60|ITUB4 38|BBDC4 17|BBAS3 22|ABEV3 13|WEGE3 38|B3SA3 13|RENT3 42|RADL3 25|SUZB3 52|ELET3 48|NU 15|MELI 2200|SQM 45
WALMEX 56|AMXB 19|GFNORTEO 175|FEMSAUBD 165|GMEXICOB 130|CEMEXCPO 17|ASURB 560|GAPB 420|BIMBOA 58
`
export const REAL_PRICES: Record<string, number> = Object.fromEntries(
  REAL_PRICES_RAW.trim()
    .split(/[|\n]/)
    .map((x) => x.trim().split(' '))
    .map(([t, p]) => [t, Number(p)]),
)

/* ── synthetic breadth (small/mid caps) so the screener exercises virtualization ── */

const PREFIX_BY_REGION: Record<RegionCode, string[]> = {
  US: ['Northgate', 'Halden', 'Brightwater', 'Crescent', 'Summit', 'Ironbridge', 'Bluecrest', 'Redwood', 'Meridian', 'Cobalt', 'Pinnacle', 'Harbor', 'Silverline', 'Oakmont', 'Granite', 'Lakeshore', 'Westbrook', 'Evergreen', 'Stonegate', 'Keystone', 'Clearwater', 'Falcon', 'Vantage', 'Sterling', 'Beacon', 'Carver', 'Ember', 'Prairie', 'Copperline', 'Tidewater'],
  EU: ['Rheinland', 'Nordwerk', 'Alpina', 'Lusitana', 'Ibérica', 'Adriatica', 'Baltica', 'Helvetia', 'Flandria', 'Batavia', 'Scandia', 'Tagus', 'Douro', 'Danubia', 'Provence', 'Lombarda', 'Castilla', 'Fjord', 'Aurora', 'Hansa'],
  UK: ['Albion', 'Pennine', 'Thameside', 'Caledonian', 'Cotswold', 'Mercia', 'Wessex', 'Severn', 'Highland', 'Kingsbridge', 'Ashford', 'Burlington'],
  JP: ['Tokai', 'Kansai', 'Hokuriku', 'Chubu', 'Daiwa', 'Sanyo', 'Shikoku', 'Kyushu', 'Toho', 'Nippon', 'Kanto', 'Tohoku', 'Hokkaido', 'Seibu'],
  KR: ['Hanil', 'Daesung', 'Dongkuk', 'Hansol', 'Samyang', 'Kumho', 'Seah', 'Hyosung', 'Taekwang', 'Poongsan'],
  CNHK: ['Jiangsu', 'Zhejiang', 'Shenzhen', 'Guangdong', 'Anhui', 'Sichuan', 'Shandong', 'Fujian', 'Kowloon', 'Victoria Harbour', 'Hubei', 'Yunnan'],
  LATAM: ['Paulista', 'Minas', 'Atlântico', 'Andina', 'Azteca', 'Norte', 'Bajío', 'Pampa', 'Cerrado', 'Pacífico'],
  OTHER: [],
}
const COUNTRIES_BY_REGION: Record<RegionCode, [string, string][]> = {
  US: [['US', 'NYSE'], ['US', 'NASDAQ']],
  EU: [['DE', 'XETRA'], ['FR', 'EPA'], ['NL', 'AMS'], ['ES', 'BME'], ['IT', 'MIL'], ['PT', 'ELI'], ['CH', 'SIX'], ['SE', 'STO'], ['DK', 'CPH'], ['NO', 'OSL'], ['FI', 'HEL'], ['BE', 'EBR']],
  UK: [['GB', 'LSE']],
  JP: [['JP', 'TSE']],
  KR: [['KR', 'KRX']],
  CNHK: [['HK', 'HKEX'], ['CN', 'HKEX'], ['CN', 'SSE'], ['CN', 'SZSE']],
  LATAM: [['BR', 'B3'], ['MX', 'BMV'], ['BR', 'B3']],
  OTHER: [],
}
const LEGAL_SUFFIX: Record<string, string> = {
  US: 'Inc.', DE: 'AG', FR: 'SA', NL: 'N.V.', ES: 'SA', IT: 'SpA', PT: 'SGPS SA', CH: 'AG', SE: 'AB', DK: 'A/S', NO: 'ASA', FI: 'Oyj', BE: 'NV',
  GB: 'plc', JP: 'Co., Ltd.', KR: 'Co., Ltd.', CN: 'Co., Ltd.', HK: 'Holdings Ltd.', BR: 'SA', MX: 'SAB de CV',
}
const SYNTH_COUNT: Record<RegionCode, number> = { US: 280, EU: 140, UK: 60, JP: 100, KR: 45, CNHK: 60, LATAM: 35, OTHER: 0 }

/* ── internal record ── */

export interface UniverseEntry extends CompanySummary {
  synthetic: boolean
  fxToUsd: number
  reportingCurrency: string
  ret1m: number
  ret12m: number
  beta: number
  kpis: Record<string, number>
  growthBase: number
  marginBase: number
  revenueUsdBn: number
}

function bucket(mcapUsd: number): MarketCapBucket {
  const b = mcapUsd / 1e9
  if (b >= 200) return 'mega'
  if (b >= 10) return 'large'
  if (b >= 2) return 'mid'
  if (b >= 0.3) return 'small'
  return 'micro'
}

const PRICE_RANGE: Record<string, [number, number]> = {
  USD: [18, 650], EUR: [8, 420], GBP: [1.2, 75], CHF: [40, 380], DKK: [250, 1100], SEK: [90, 520], NOK: [110, 360],
  JPY: [700, 42000], KRW: [18000, 900000], HKD: [4, 480], CNY: [5, 1750], BRL: [6, 62], MXN: [18, 260],
}

function isinFor(country: string, seed: string): string {
  const r = new Rng(`isin:${seed}`)
  const body = Array.from({ length: 9 }, () => r.pick('0123456789ABCDEFGHJKLMNPQRSTUVWXYZ'.split(''))).join('')
  const base = `${country === 'HK' ? 'HK' : country}${body}`
  // ISIN check digit (Luhn over digit-expanded string)
  const digits = base
    .split('')
    .map((ch) => (/[0-9]/.test(ch) ? ch : String(ch.charCodeAt(0) - 55)))
    .join('')
  let sum = 0
  for (let i = 0; i < digits.length; i++) {
    let d = Number(digits[digits.length - 1 - i])
    if (i % 2 === 0) {
      d *= 2
      if (d > 9) d -= 9
    }
    sum += d
  }
  return `${base}${(10 - (sum % 10)) % 10}`
}

function clampKpi(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v))
}

function makeEntry(
  ticker: string,
  name: string,
  exKey: string,
  country: string,
  subId: string,
  mcapBn: number,
  synthetic: boolean,
): UniverseEntry {
  const ex = EX[exKey]
  const meta = COUNTRIES[country]
  const { sector, sub } = SUBSECTOR_INDEX[subId]
  const tradingCurrency = ex.usd ? 'USD' : meta.currency
  const reportingCurrency = ex.usd && meta.region !== 'US' ? (meta.currency === 'BRL' ? 'BRL' : 'USD') : meta.currency
  const id = `${ticker.replace(/\.$/, '').replace('.', '-')}${ex.suffix}`
  const r = new Rng(`co:${id}`)
  const profile: KpiProfile = sub.profile
  const [plo, phi] = PRICE_RANGE[tradingCurrency] ?? [10, 300]
  const randomPrice = Math.exp(r.range(Math.log(plo), Math.log(phi)))
  const known = synthetic ? undefined : REAL_PRICES[ticker]
  const price = round(known ?? randomPrice, tradingCurrency === 'JPY' || tradingCurrency === 'KRW' ? 0 : 2)
  const marketCapUsd = mcapBn * 1e9
  const quality = r.next()
  const sizeTilt = Math.min(1, Math.log10(mcapBn + 1) / 3.5)
  const moatScore = 0.55 * quality + 0.35 * sizeTilt + 0.1 * r.next()
  const moat: MoatRating = moatScore > 0.68 ? 'wide' : moatScore > 0.42 ? 'narrow' : 'none'
  const dq: DataQuality =
    meta.region === 'US' || meta.region === 'UK' || meta.region === 'EU'
      ? r.chance(0.85) ? 'high' : 'medium'
      : r.chance(0.55) ? 'medium' : r.chance(0.6) ? 'high' : 'low'

  const kpis: Record<string, number> = {}
  for (const spec of KPI_SETS[profile]) {
    const tilt = spec.higherIsBetter ? quality - 0.5 : 0.5 - quality
    kpis[spec.id] = round(clampKpi(spec.mu + spec.sd * (r.normal() * 0.85 + tilt * 0.9), spec.lo, spec.hi), 4)
  }
  // generic metrics used by screener columns (all profiles)
  const roic = clampKpi(0.1 + 0.12 * (quality - 0.5) + r.normal(0, 0.05), -0.2, 0.6)
  const growthBase = clampKpi(0.05 + r.normal(0, 0.06) + (subId === 'software' || subId === 'semis' ? 0.08 : 0), -0.2, 0.6)
  const marginBase = clampKpi(0.14 + 0.1 * (quality - 0.5) + r.normal(0, 0.05), -0.1, 0.55)
  kpis.roic ??= round(roic, 4)
  kpis.revenueGrowth ??= round(growthBase, 4)
  kpis.peNtm ??= round(clampKpi(r.normal(17, 6), 5, 70), 1)
  kpis.evEbit ??= round(clampKpi(r.normal(15, 6), 3, 70), 1)
  kpis.fcfYield ??= round(clampKpi(r.normal(0.045, 0.025), -0.05, 0.2), 4)
  kpis.netDebtEbitda ??= round(clampKpi(r.normal(1.4, 1.1), -2, 7), 2)
  kpis.dividendYield ??= round(clampKpi(r.normal(0.025, 0.015), 0, 0.1), 4)

  const ret12m = r.normal(0.08, 0.25)
  const ret1m = r.normal(0.008, 0.065)
  const raw: Record<FactorFamily, number> = {
    quality: quality * 0.75 + r.next() * 0.25,
    profitability: quality * 0.6 + r.next() * 0.4,
    growth: (growthBase + 0.2) / 0.8 + r.normal(0, 0.15),
    value: r.next() * 0.8 + (1 - quality) * 0.2,
    balanceSheet: r.next() * 0.6 + quality * 0.4,
    momentum: ret12m + r.normal(0, 0.1),
    insider: r.next(),
    sentiment: r.next() * 0.7 + (ret1m + 0.1) * 1.5,
    macro: r.next(),
    moat: moatScore + r.normal(0, 0.08),
  }
  return {
    id,
    ticker: ticker.replace(/\.$/, ''),
    name,
    exchange: ex.name,
    isin: isinFor(country, id),
    country,
    region: meta.region,
    currency: tradingCurrency,
    sector: sector.name,
    subsector: sub.name,
    kpiProfile: profile,
    marketCapUsd,
    marketCapBucket: bucket(marketCapUsd),
    price,
    change1dPct: round(r.normal(0.0004, 0.016), 4),
    compositeScore: 0,
    // temporarily store raw scores; converted to percentiles in buildUniverse()
    factorScores: raw as FactorScores,
    moat,
    dataQuality: dq,
    synthetic,
    fxToUsd: FX_TO_USD[tradingCurrency] ?? 1,
    reportingCurrency,
    ret1m: round(ret1m, 4),
    ret12m: round(ret12m, 4),
    beta: round(clampKpi(r.normal(1, 0.25), 0.3, 2.2), 2),
    kpis,
    growthBase,
    marginBase,
    // price/sales: banks & retailers trade lower, software/semis higher
    revenueUsdBn: round(
      mcapBn /
        clampKpi(
          r.normal(profile === 'software' || profile === 'semis' ? 7 : profile === 'retail' || profile === 'energy' ? 1.2 : 2.6, 1),
          0.5,
          18,
        ),
      2,
    ),
  }
}

function synthTicker(r: Rng, region: RegionCode, exKey: string, used: Set<string>): string {
  for (let i = 0; i < 50; i++) {
    let t: string
    const L = () => r.pick('ABCDEFGHIJKLMNOPRSTUVWXYZ'.split(''))
    switch (region) {
      case 'JP':
        t = String(r.int(1301, 9997))
        break
      case 'KR':
        t = String(r.int(1000, 399999)).padStart(6, '0')
        break
      case 'CNHK':
        t = exKey === 'HKEX' ? String(r.int(1, 9999)).padStart(4, '0') : exKey === 'SSE' ? `60${r.int(1000, 3999)}` : `00${r.int(1000, 2999)}`
        break
      case 'LATAM':
        t = exKey === 'B3' ? `${L()}${L()}${L()}${L()}3` : `${L()}${L()}${L()}${L()}${r.chance(0.5) ? 'B' : 'A'}`
        break
      default:
        t = Array.from({ length: r.int(3, 4) }, L).join('')
    }
    if (!used.has(`${exKey}:${t}`)) {
      used.add(`${exKey}:${t}`)
      return t
    }
  }
  return `X${used.size}`
}

function buildUniverse(): UniverseEntry[] {
  const out: UniverseEntry[] = []
  const used = new Set<string>()
  for (const line of SEED.trim().split('\n')) {
    const [t, n, ex, cc, sub, cap] = line.split('|')
    used.add(`${ex}:${t}`)
    out.push(makeEntry(t, n, ex, cc, sub, Number(cap), false))
  }
  const r = new Rng('synthetic-universe-v1')
  const subs = Object.keys(SUBSECTOR_INDEX)
  const usedNames = new Set(out.map((e) => e.name))
  for (const region of Object.keys(SYNTH_COUNT) as RegionCode[]) {
    for (let i = 0; i < SYNTH_COUNT[region]; i++) {
      const [cc, ex] = r.pick(COUNTRIES_BY_REGION[region])
      const subId = r.pick(subs)
      const { sub } = SUBSECTOR_INDEX[subId]
      let name = ''
      for (let a = 0; a < 10 && (!name || usedNames.has(name)); a++) {
        name = `${r.pick(PREFIX_BY_REGION[region])} ${r.pick(sub.words)} ${LEGAL_SUFFIX[cc] ?? ''}`.trim()
      }
      usedNames.add(name)
      const mcap = round(Math.exp(r.normal(Math.log(3.2), 1.1)), 2)
      out.push(makeEntry(synthTicker(r, region, ex, used), name, ex, cc, subId, Math.max(0.08, Math.min(mcap, 80)), true))
    }
  }
  // convert raw factor values → cross-sectional percentiles (0–100)
  for (const f of FACTOR_FAMILIES) {
    const sorted = out.map((e, i) => [e.factorScores[f], i] as const).sort((a, b) => a[0] - b[0])
    sorted.forEach(([, idx], rank) => {
      out[idx].factorScores[f] = round(((rank + 0.5) / sorted.length) * 100, 1)
    })
  }
  for (const e of out) e.compositeScore = compositeOf(e.factorScores, DEFAULT_WEIGHTS)
  return out
}

export function compositeOf(scores: FactorScores, weights: Record<FactorFamily, number>): number {
  let s = 0
  let w = 0
  for (const f of FACTOR_FAMILIES) {
    s += scores[f] * weights[f]
    w += weights[f]
  }
  return w > 0 ? round(s / w, 1) : 0
}

export const UNIVERSE: UniverseEntry[] = buildUniverse()
export const BY_ID: Map<string, UniverseEntry> = new Map(UNIVERSE.map((e) => [e.id, e]))

export function toSummary(e: UniverseEntry): CompanySummary {
  return {
    id: e.id,
    ticker: e.ticker,
    name: e.name,
    exchange: e.exchange,
    isin: e.isin,
    country: e.country,
    region: e.region,
    currency: e.currency,
    sector: e.sector,
    subsector: e.subsector,
    kpiProfile: e.kpiProfile,
    marketCapUsd: e.marketCapUsd,
    marketCapBucket: e.marketCapBucket,
    price: e.price,
    change1dPct: e.change1dPct,
    compositeScore: e.compositeScore,
    factorScores: { ...e.factorScores },
    moat: e.moat,
    dataQuality: e.dataQuality,
  }
}

export function toScreenerRow(e: UniverseEntry, rank: number): ScreenerRow {
  const h = hashString(e.id)
  return {
    ...toSummary(e),
    rank,
    return1m: e.ret1m,
    return12m: e.ret12m,
    peNtm: e.kpis.peNtm ?? null,
    evEbit: e.kpis.evEbit ?? null,
    fcfYield: e.kpis.fcfYield ?? null,
    roic: e.kpiProfile === 'bank' || e.kpiProfile === 'insurance' ? null : (e.kpis.roic ?? null),
    revenueGrowth: e.kpiProfile === 'bank' ? null : (e.kpis.revenueGrowth ?? null),
    netDebtEbitda: e.kpiProfile === 'bank' || e.kpiProfile === 'insurance' ? null : (e.kpis.netDebtEbitda ?? null),
    dividendYield: e.kpis.dividendYield ?? null,
    modelExpectedExcess: round(((e.compositeScore - 50) / 50) * 0.06 + (((h % 1000) / 1000 - 0.5) * 0.03), 4),
    // macro betas exist only in the live API (model-based, macro-w4)
    mxBetaRates: null,
    mxBetaUsd: null,
    mxBetaOil: null,
    mxBetaCredit: null,
    mxBetaGrowthSurprise: null,
    mxImpactRatesDown100: null,
  }
}
