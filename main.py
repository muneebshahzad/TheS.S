import asyncio
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, flash, redirect, url_for, abort
import datetime, requests
from datetime import datetime
import pymssql, shopify
import aiohttp
import lazop
import aiohttp

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')  # Use environment variable
pre_loaded = 0
order_details = []
daraz_orders = []

def get_db_connection():
    server = os.getenv('DB_SERVER')
    database = os.getenv('DB_DATABASE')
    username = os.getenv('DB_USERNAME')
    password = os.getenv('DB_PASSWORD')
    try:
        connection = pymssql.connect(server=server, user=username, password=password, database=database)
        return connection
    except pymssql.Error as e:
        print(f"Error connecting to the database: {str(e)}")
        return None


@app.route('/send-email', methods=['POST'])
def send_email():
    data = request.get_json()
    to_emails = data.get('to', [])
    cc_emails = data.get('cc', [])
    subject = data.get('subject', '')
    body = data.get('body', '')

    try:
        # SMTP server configuration
        smtp_server = 'smtp.gmail.com'
        smtp_port = 587
        smtp_user = os.getenv('SMTP_USER')  # Use environment variable
        smtp_password = os.getenv('SMTP_PASSWORD')  # Use environment variable

        # Create the message
        msg = MIMEText(body)
        msg['From'] = smtp_user
        msg['To'] = ', '.join(to_emails)
        msg['Cc'] = ', '.join(cc_emails)
        msg['Subject'] = subject

        # Connect to the SMTP server and send email
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_emails + cc_emails, msg.as_string())
        server.quit()

        return jsonify({'message': 'Email sent successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/resume')
def resume():
    try:
        # SMTP server configuration
        smtp_server = 'smtp.gmail.com'
        smtp_port = 587
        smtp_user = os.getenv('SMTP_USER')  # Use environment variable for SMTP username
        smtp_password = os.getenv('SMTP_PASSWORD')  # Use environment variable for SMTP password

        # Create the message
        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = "muneeb.shahzad101@gmail.com"
        msg['Subject'] = "Your Resume Was Seen"

        # Email body
        body = "RESUME SEEN"
        msg.attach(MIMEText(body, 'plain'))

        # Connect to the SMTP server and send email
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, msg['To'], msg.as_string())
        server.quit()

        print("Email sent successfully")
    except Exception as e:
        print(f"Email not sent. Error: {e}")

    return render_template('resume.html')


async def fetch_tracking_data(session, tracking_number):
    api_key = os.getenv('LEOPARD_API_KEY')
    api_password = os.getenv('LEOPARD_PASSWORD')
    url = f"https://merchantapi.leopardscourier.com/api/trackBookedPacket/?api_key={api_key}&api_password={api_password}&track_numbers={tracking_number}"
    async with session.get(url) as response:
        return await response.json()


async def process_line_item(session, line_item, fulfillments):
    if line_item.fulfillment_status is None and line_item.fulfillable_quantity == 0:
        return []

    tracking_info = []
    name = 'N/A'
    address = 'N/A'
    phone = 'N/A'
    city = 'N/A'
    if line_item.fulfillment_status == "fulfilled":
        for fulfillment in fulfillments:
            if fulfillment.status == "cancelled":
                continue
            for item in fulfillment.line_items:
                if item.id == line_item.id:
                    tracking_number = fulfillment.tracking_number
                    data = await fetch_tracking_data(session, tracking_number)
                    if data['status'] == 1 and not data['error']:
                        packet_list = data['packet_list']
                        if packet_list:
                            name = packet_list[0]['consignment_name_eng']
                            address = packet_list[0]['consignment_address']
                            phone = packet_list[0]['consignment_phone']
                            city = packet_list[0]['destination_city_name']
                            tracking_details = packet_list[0].get('Tracking Detail', [])
                            if tracking_details:
                                final_status = packet_list[0]['booked_packet_status']
                                keywords = ["Return", "hold", "UNTRACEABLE"]
                                if not any(
                                        kw.lower() in final_status.lower() for kw in
                                        ["delivered", "returned to shipper"]):
                                    for detail in tracking_details:
                                        status = detail['Status']
                                        if status == 'Pending':
                                            reason = detail['Reason']
                                        else:
                                            reason = 'N/A'
                                        if any(kw in status for kw in keywords) or any(kw in reason for kw in keywords):
                                            final_status = "Being Return"
                                            break
                            else:
                                final_status = "Booked"
                                print("No tracking details available.")
                        else:
                            final_status = "Booked"
                            print("No packets found.")
                    else:
                        final_status = "N/A"
                        print("Error fetching data.")

                    # Track quantity for each tracking number
                    tracking_info.append({
                        'tracking_number': tracking_number,
                        'status': final_status,
                        'quantity': item.quantity,
                        'name': name,
                        'address': address,
                        'city': city,
                        "phone": phone,
                    })

    return tracking_info if tracking_info else [
        {"tracking_number": "N/A", "status": "Un-Booked", name: 'N/A', address: 'N/A', phone: 'N/A', city: 'N/A',
         "quantity": line_item.quantity}]


async def process_order(session, order):
    order_start_time = time.time()

    input_datetime_str = order.created_at
    parsed_datetime = datetime.fromisoformat(input_datetime_str[:-6])
    formatted_datetime = parsed_datetime.strftime("%b %d, %Y")

    try:
        status = (order.fulfillment_status).title()
    except:
        status = "Un-fulfilled"
    print(order)
    tags = []
    try:
        name = order.billing_address.name
    except AttributeError:
        name = " "
        print("Error retrieving name")

    try:
        address = order.billing_address.address1
    except AttributeError:
        address = " "
        print("Error retrieving address")

    try:
        city = order.billing_address.city
    except AttributeError:
        city = " "
        print("Error retrieving city")

    try:
        phone = order.billing_address.phone
    except AttributeError:
        phone = " "
        print("Error retrieving phone")

    customer_details = {
        "name": name,
        "address": address,
        "city": city,
        "phone": phone
    }
    order_info = {
        'order_id': order.name,
        'tracking_id': 'N/A',
        'created_at': formatted_datetime,
        'total_price': order.total_price,
        'line_items': [],
        'financial_status': (order.financial_status).title(),
        'fulfillment_status': status,
        'customer_details' : customer_details,
        'tags': order.tags.split(", "),
        'id': order.id
    }
    print(order.tags)

    tasks = []
    for line_item in order.line_items:
        tasks.append(process_line_item(session, line_item, order.fulfillments))

    results = await asyncio.gather(*tasks)
    variant_name = ""
    for tracking_info_list, line_item in zip(results, order.line_items):
        if tracking_info_list is None:
            continue

        if line_item.product_id is not None:
            product = shopify.Product.find(line_item.product_id)
            if product and product.variants:
                for variant in product.variants:
                    if variant.id == line_item.variant_id:
                        if variant.image_id is not None:
                            images = shopify.Image.find(image_id=variant.image_id, product_id=line_item.product_id)
                            variant_name = line_item.variant_title
                            for image in images:
                                if image.id == variant.image_id:
                                    image_src = image.src
                        else:
                            variant_name = ""
                            image_src = product.image.src
        else:
            image_src = "https://static.thenounproject.com/png/1578832-200.png"

        for info in tracking_info_list:
            order_info['line_items'].append({
                'fulfillment_status': line_item.fulfillment_status,
                'image_src': image_src,
                'product_title': line_item.title + " - " + variant_name,
                'quantity': info['quantity'],
                'tracking_number': info['tracking_number'],
                'status': info['status'],
                'name': info.get('name', 'N/A'),
                'address': info.get('address', 'N/A'),
                'city': info.get('city', 'N/A'),
                'phone': info.get('phone', 'N/A'),
            })
            order_info['status'] = info['status']

    order_end_time = time.time()
    print(f"Time taken to process order {order.order_number}: {order_end_time - order_start_time:.2f} seconds")

    return order_info


@app.route('/apply_tag', methods=['POST'])
def apply_tag():
    data = request.json
    order_id = data.get('order_id')
    tag = data.get('tag')

    # Get today's date in YYYY-MM-DD format
    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag.strip()} ({today_date})"

    try:
        # Fetch the order
        order = shopify.Order.find(order_id)

        # If the tag is "Returned", cancel the order
        if tag.strip().lower() == "returned":
            # Attempt to cancel the order
            if order.cancel():
                print("Order Cancelled")
            else:
                print("Order Cancellation Failed")
        if tag.strip().lower() == "delivered":
            if order.close():
                print("Order Cloed")
            else:
                print("Order Closing Failed")

        # Process existing tags
        if order.tags:
            tags = [t.strip() for t in order.tags.split(", ")]  # Remove excess spaces
        else:
            tags = []

        # Remove a specific tag if needed (e.g., "Leopards Courier")
        if "Leopards Courier" in tags:
            tags.remove("Leopards Courier")

        # Add new tag if it doesn't already exist
        if tag_with_date not in tags:
            tags.append(tag_with_date)

        # Update the order with the new tags
        order.tags = ", ".join(tags)

        # Save the order
        if order.save():
            return jsonify({"success": True, "message": "Tag applied successfully."})
        else:
            return jsonify({"success": False, "error": "Failed to save order changes."})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)})


async def getShopifyOrders():
    global order_details
    orders = shopify.Order.find(limit=250, order='created_at DESC')
    order_details = []
    total_start_time = time.time()

    async with aiohttp.ClientSession() as session:
        tasks = [process_order(session, order) for order in orders]
        order_details = await asyncio.gather(*tasks)

    total_end_time = time.time()
    print(f"Total time taken to process all orders: {total_end_time - total_start_time:.2f} seconds")

    return order_details


@app.route("/track")
def tracking():
    global order_details, pre_loaded,daraz_orders
    return render_template("track.html", order_details=order_details,darazOrders=daraz_orders)


def get_daraz_orders(statuses):
    try:
        access_token = '50000902021Yejq5rSzhGiOp0hRUGDxjt12fb1602qewR3toNX4APiKiju3bxu'
        client = lazop.LazopClient('https://api.daraz.pk/rest', '501554', 'nrP3XFN7ChZL53cXyVED1yj4iGZZtlcD')

        all_orders = []

        for status in statuses:
            request = lazop.LazopRequest('/orders/get', 'GET')
            request.add_api_param('sort_direction', 'DESC')
            request.add_api_param('update_before', '2025-02-10T16:00:00+08:00')
            request.add_api_param('offset', '0')
            request.add_api_param('created_before', '2025-02-10T16:00:00+08:00')
            request.add_api_param('created_after', '2017-02-10T09:00:00+08:00')
            request.add_api_param('limit', '50')
            request.add_api_param('update_after', '2017-02-10T09:00:00+08:00')
            request.add_api_param('sort_by', 'updated_at')
            request.add_api_param('status', status)
            request.add_api_param('access_token', access_token)

            response = client.execute(request)
            darazOrders = response.body.get('data', {}).get('orders', [])

            for order in darazOrders:
                order_id = order.get('order_id', 'Unknown')

                item_request = lazop.LazopRequest('/order/items/get', 'GET')
                item_request.add_api_param('order_id', order_id)
                item_request.add_api_param('access_token', access_token)

                item_response = client.execute(item_request)
                items = item_response.body.get('data', [])


                item_details = []
                for item in items:
                    tracking_num = item.get('tracking_code', 'Unknown')

                    tracking_req = lazop.LazopRequest('/logistic/order/trace', 'GET')
                    tracking_req.add_api_param('order_id', order_id)
                    tracking_req.add_api_param('access_token', access_token)
                    tracking_response = client.execute(tracking_req)

                    tracking_data = tracking_response.body.get('result', {})
                    packages = tracking_data.get('data', [{}])[0].get('package_detail_info_list', [])

                    track_status = "N/A"
                    for package in packages:
                        if package.get("tracking_number") == tracking_num:
                            track_status = package.get('logistic_detail_info_list', [{}])[-1].get('title', "N/A")
                            break
                    product_title = f"{item.get('name', 'Unknown')} {item.get('variation', 'N/A')}"
                    if "Color family:" in product_title:
                        product_info, color_info = product_title.split("Color family:", 1)
                        product_title = f"{product_info.strip()} - {color_info.strip()}"

                    item_detail = {
                        'item_image': item.get('product_main_image', 'N/A'),
                        'item_title': product_title,
                        'quantity': 1,
                        'tracking_number': item.get('tracking_code', 'N/A'),
                        'status': track_status
                    }
                    item_details.append(item_detail)

                filtered_order = {
                    'order_id': f"{order.get('order_id', 'Unknown')}",
                    'customer': {
                        'name': f"{order.get('customer_first_name', 'Unknown')} {order.get('customer_last_name', 'Unknown')}",
                        'address': order.get('address_shipping', {}).get('address', 'N/A'),
                        'phone': order.get('address_shipping', {}).get('phone', 'N/A')
                    },
                    'status': status.replace('_', ' ').title(),
                    'date': format_date(order.get('created_at', 'N/A')),
                    'total_price': order.get('price', '0.00'),
                    'items_list': item_details,
                    'tracking_id': 'N/A',
                }
                all_orders.append(filtered_order)

        return all_orders
    except Exception as e:
        print(f"Error fetching darazOrders: {e}")
        return []



@app.route('/daraz')
def daraz():
    statuses = ['shipped', 'pending', 'ready_to_ship']
    darazOrders = get_daraz_orders(statuses)
    return render_template('daraz.html', darazOrders=darazOrders)


def format_date(date_str):
    # Parse the date string
    date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %z")
    # Format the date object to only show the date
    return date_obj.strftime("%Y-%m-%d")


@app.route('/refresh', methods=['POST'])
def refresh_data():
    global order_details
    try:
        order_details = asyncio.run(getShopifyOrders())
        return render_template("track.html", order_details=order_details)
    except Exception as e:
        print(f"Error refreshing data: {e}")
        return jsonify({'message': 'Failed to refresh data'}), 500


def check_database_connection():
    server = 'tickbags.database.windows.net'
    database = 'TickBags'
    username = 'tickbags_ltd'
    password = 'TB@2024!'

    try:
        print('Connecting to the database...')
        connection = pymssql.connect(server=server, user=username, password=password, database=database)

        print('Connected to the database')
        return connection
    except pymssql.Error as e:
        print(f"Error connecting to the database: {str(e)}")
        time.sleep(5)
        check_database_connection()
        return None


def fetch_transaction_data():
    connection = check_database_connection()
    if connection is None:
        return []
    print("CONNECTED TO DATABASE")

    try:
        with connection.cursor(as_dict=True) as cursor:
            query = '''SELECT * FROM IncomeExpenseTable ORDER BY "Payment_Date" desc'''
            cursor.execute(query)
            transactions = cursor.fetchall()
            return transactions
    except pymssql.Error as e:
        print(f"Error fetching data from the database: {str(e)}")
        return []
    finally:
        connection.close()


@app.route('/finance_report')
def finance_report():
    transactions = fetch_transaction_data()

    return render_template('finance_report.html', transactions=transactions)


def fetch_monthly_financial_data(connection):
    cursor = connection.cursor()

    try:
        cursor.execute("SELECT Month, NetProfit FROM MonthlySummary ORDER BY Month ASC")
        financial_data = cursor.fetchall()

        formatted_data = {
            'months': [row[0] for row in financial_data],
            'net_amounts': [row[1] for row in financial_data]
        }

        return formatted_data

    except Exception as e:
        print(f"Error fetching monthly financial data: {str(e)}")
        return {'months': [], 'net_amounts': []}

    finally:
        cursor.close()


def fetch_account_summary(connection):
    cursor = connection.cursor()

    try:
        # Fetch Cash on Hand (assuming it's stored in the 'accounts' table)
        cursor.execute(
            "SELECT FORMAT(accounts_balance, 'N0') as FormattedAmount   FROM accounts WHERE accounts_name='Bank'")
        cash_on_hand = cursor.fetchone()[0]

        # Fetch Earnings (Monthly)
        cursor.execute("""
            SELECT FORMAT(Income, 'N0') as FormattedAmount
            FROM MonthlySummary
            WHERE [Month] = FORMAT(GETDATE(), 'yyyy-MM')
        """)
        earnings_monthly = cursor.fetchone()[0] or 0

        # Fetch Expenses (Monthly)
        cursor.execute("""
            SELECT FORMAT(Expense, 'N0') as FormattedAmount
            FROM MonthlySummary
            WHERE [Month] = FORMAT(GETDATE(), 'yyyy-MM')
        """)
        expenses_monthly = cursor.fetchone()[0] or 0

        # Calculate Net Profit (Including Withdrawal)
        cursor.execute("""
            SELECT FORMAT(NetProfit, 'N0') AS FormattedAmount
            FROM MonthlySummary
            WHERE [Month] = FORMAT(GETDATE(), 'yyyy-MM')
        """)
        net_profit = cursor.fetchone()[0] or 0

        return {
            'cash_on_hand': cash_on_hand,
            'earnings_monthly': earnings_monthly,
            'expenses_monthly': expenses_monthly,
            'net_profit': net_profit
        }

    except Exception as e:
        print(f"Error fetching account summary: {str(e)}")
        return {}

    finally:
        cursor.close()


def fetch_accounts_data(connection):
    cursor = connection.cursor()

    try:
        cursor.execute(
            'SELECT accounts_name, accounts_balance FROM accounts order by accounts_balance desc')  # Adjust the query accordingly
        accounts_data = cursor.fetchall()

        formatted_accounts = []

        for row in accounts_data:
            formatted_account = {
                'person_name': row[0],
                'balance': int(row[1]),

            }

            formatted_accounts.append(formatted_account)

        return formatted_accounts

    except Exception as e:
        print(f"Error fetching accounts data: {str(e)}")
        return []

    finally:
        cursor.close()


def fetch_income_list(connection):
    cursor = connection.cursor()

    try:
        # Execute the SQL query
        query = '''

SELECT TOP 5
    Income_Expense_Name,
    SUM(CAST(Amount AS FLOAT)) AS Amount
FROM IncomeExpenseTable
WHERE Type = 'Income'
    AND FORMAT(CONVERT(datetime, Payment_Date, 120), 'yyyy-MM') = FORMAT(GETDATE(), 'yyyy-MM')
GROUP BY Income_Expense_Name
ORDER BY Amount DESC


        '''
        cursor.execute(query)
        summary_data = cursor.fetchall()

        formatted_data = {
            'income': [row[0] for row in summary_data],
            'net_amounts': [row[1] for row in summary_data]
        }

        return formatted_data


    except Exception as e:
        print(f"Error fetching income and expense summary: {str(e)}")
        return [], []

    finally:
        cursor.close()


def fetch_expenses(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("""
                   SELECT TOP 5
    Income_Expense_Name,
    SUM(CAST(Amount AS FLOAT)) AS Amount
FROM IncomeExpenseTable
WHERE Type = 'Expense'
    AND FORMAT(CONVERT(datetime, Payment_Date, 120), 'yyyy-MM') = FORMAT(GETDATE(), 'yyyy-MM')
GROUP BY Income_Expense_Name
ORDER BY Amount DESC;

                """, )

        summary_data = cursor.fetchall()
        formatted_data = {
            'expense': [row[0] for row in summary_data],
            'net_amounts': [row[1] for row in summary_data]
        }

        return formatted_data

    except Exception as e:
        print(f"Error fetching incomes data: {str(e)}")
        return []

    finally:
        cursor.close()


@app.route('/')
def accounts():
    connection = check_database_connection()

    try:
        if not connection:
            connection = check_database_connection()

        if connection:
            financial_data = fetch_monthly_financial_data(connection)
            accounts = fetch_accounts_data(connection)
            account_summary = fetch_account_summary(connection)
            income_data = fetch_income_list(connection)
            expense_data = fetch_expenses(connection)

            # Debugging print statements
            print("Financial Data:", financial_data)
            print("Accounts:", accounts)
            print("Account Summary:", account_summary)
            print("Income Data:", income_data)
            print("Expense Data:", expense_data)

            colors = ['#4e73df', '#1cc88a', '#36b9cc', '#f6c23e', '#e74a3b', '#858796']

            # Handle empty income or expense data gracefully
            income_list = income_data.get('income', [])
            expense_list = expense_data.get('expense', [])

            # Ensure the colors don't exceed the income/expense list lengths
            labeled_colors = list(zip(income_list, colors[:len(income_list)])) if income_list else []
            labeled_expenses_colors = list(zip(expense_list, colors[:len(expense_list)])) if expense_list else []

            print("Labeled Colors:", labeled_colors)
            print("Labeled Expenses Colors:", labeled_expenses_colors)

            return render_template('accounts.html',
                                   labeled_colors=labeled_colors,
                                   colors=colors,
                                   accounts=accounts,
                                   account_summary=account_summary,
                                   financial_data=financial_data,
                                   income_data=income_data,
                                   expense_data=expense_data,
                                   labeled_expenses_colors=labeled_expenses_colors)
        else:
            return render_template('error.html', message="Could not connect to the database. Please try again later.")

    except Exception as e:
        print(f"Error in account_balances route: {str(e)}")
        return render_template('error.html', message="An unexpected error occurred. Please try again later.")

    finally:
        if connection:
            connection.close()


@app.route('/addTransaction')
def addTransaction():
    return render_template('addTransaction.html')


def run_async(func, *args, **kwargs):
    return asyncio.run(func(*args, **kwargs))


@app.route('/track/<tracking_num>')
def displayTracking(tracking_num):
    print(f"Tracking Number: {tracking_num}")  # Debug line

    async def async_func():
        async with aiohttp.ClientSession() as session:
            return await fetch_tracking_data(session, tracking_num)

    data = run_async(async_func)

    return render_template('trackingdata.html', data=data)


@app.route('/accounts/<account_name>')
def accountData(account_name):
    print(f"Account Name: {account_name}")  # Debug line

    connection = check_database_connection()
    if connection is None:
        return "Database connection error", 500  # Return an error message or page if connection fails

    print("CONNECTED TO DATABASE")

    try:
        with connection.cursor(as_dict=True) as cursor:
            query = "SELECT * FROM IncomeExpenseTable WHERE Income_Expense_Name LIKE %s ORDER BY Payment_Date DESC"
            cursor.execute(query, ('%' + account_name + '%',))
            transactions = cursor.fetchall()
    except pymssql.Error as e:
        print(f"Error fetching data from the database: {str(e)}")
        transactions = []  # Ensure transactions is defined
    finally:
        connection.close()

    # Simple template rendering to verify if template works without data
    return render_template('finance_report.html', transactions=transactions)


@app.route('/expense_data')
def expense_data():
    connection = check_database_connection()

    try:
        if connection:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT e.expense_id, e.expense_title, s.subtype_title
                FROM ExpenseTypes e
                LEFT JOIN ExpenseSubtypes s ON e.expense_id = s.expense_id
            """)
            rows = cursor.fetchall()

            expense_data = {}
            for expense_id, expense_title, subtype_title in rows:
                if expense_id not in expense_data:
                    expense_data[expense_id] = {
                        "expense_title": expense_title,
                        "subtypes": []
                    }
                if subtype_title:
                    expense_data[expense_id]["subtypes"].append(subtype_title)

            # Convert the dictionary to the format needed
            response_data = {
                'types': [{'expense_id': k, 'expense_title': v['expense_title']} for k, v in expense_data.items()],
                'subtypes': {str(k): v['subtypes'] for k, v in expense_data.items()}
            }

            return jsonify(response_data)
        else:
            return "Error: No database connection"

    except Exception as e:
        print(f"Error in expense_data route: {str(e)}")
        return "Error in expense_data route"

    finally:
        if connection:
            connection.close()


@app.route('/income_data')
def income_data():
    connection = check_database_connection()

    try:
        if connection:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT e.income_id, e.income_title, s.subtype_title
                FROM incomeTypes e
                LEFT JOIN incomeSubtypes s ON e.income_id = s.income_id
            """)
            rows = cursor.fetchall()

            income_types = {}
            income_subtypes = []

            for income_id, income_title, subtype_title in rows:
                if income_title not in income_types:
                    income_types[income_title] = income_id
                if subtype_title:
                    income_subtypes.append({
                        'subtype_title': subtype_title,
                        'income_id': income_id
                    })

            # Convert the dictionary to the format needed
            response_data = {
                'types': [{'income_id': v, 'income_title': k} for k, v in income_types.items()],
                'subtypes': income_subtypes
            }

            return jsonify(response_data)
        else:
            return "Error: No database connection"

    except Exception as e:
        print(f"Error in income_data route: {str(e)}")
        return "Error in income_data route"

    finally:
        if connection:
            connection.close()


@app.route('/add_income', methods=['POST'])
def add_income():
    connection = check_database_connection()

    if connection:
        try:
            cursor = connection.cursor()

            amount = request.form['amount']
            income_title = request.form['income_type']
            payment_to = request.form['income_subtype']
            description = request.form.get('description', '')
            submission_datetime = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            income_expense_name = f"{income_title} - {payment_to}"

            cursor.execute("""
                INSERT INTO IncomeExpenseTable (Income_Expense_Name, Description, Amount, Type, [Payment_Date])
                VALUES (%s, %s, %s, %s, %s)
            """, (income_expense_name, description, amount, 'Income', submission_datetime))

            if income_title == 'Investments':
                cursor.execute("""
                    UPDATE accounts
                    SET accounts_balance = accounts_balance + %s
                    WHERE accounts_name = %s
                """, (amount, payment_to))

            # Always update the 'Bank' account
            cursor.execute("""
                            UPDATE accounts
                            SET accounts_balance = accounts_balance + %s
                            WHERE accounts_name = 'Bank'
                        """, (amount,))

            # Commit the transaction
            connection.commit()

            return jsonify({'status': 'success', 'message': 'Income successfully added!'})

        except Exception as e:
            connection.rollback()
            print(f"Error in add_income route: {str(e)}")
            return jsonify({'status': 'error', 'message': 'Error in adding income'})

        finally:
            connection.close()
    else:
        return jsonify({'status': 'error', 'message': 'Error: No database connection'})


from flask import request, jsonify
from datetime import datetime


@app.route('/add_expense', methods=['POST'])
def add_expense():
    connection = check_database_connection()

    if connection:
        try:
            cursor = connection.cursor()

            amount = float(request.form['amount'])  # Convert to float for numeric operations
            expense_title = request.form['expense_type']
            payment_to = request.form['expense_subtype']
            description = request.form.get('description', '')
            submission_datetime = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            income_expense_name = f"{expense_title} - {payment_to}"

            # Insert into IncomeExpenseTable
            cursor.execute("""
                INSERT INTO IncomeExpenseTable (Income_Expense_Name, Description, Amount, Type, [Payment_Date])
                VALUES (%s, %s, %s, %s, %s)
            """, (income_expense_name, description, amount, 'Expense', submission_datetime))

            # Update accounts if expense_title is 'Profit Withdrawal'
            if expense_title == 'Profit Withdrawal' or expense_title == 'Employee Salary' or expense_title == 'Employee Loan':
                cursor.execute("""
                    UPDATE accounts
                    SET accounts_balance = accounts_balance + %s
                    WHERE accounts_name = %s
                """, (amount, payment_to))

            # Always update the 'Bank' account
            cursor.execute("""
                UPDATE accounts
                SET accounts_balance = accounts_balance - %s
                WHERE accounts_name = 'Bank'
            """, (amount,))

            # Commit the transaction
            connection.commit()

            return jsonify({'status': 'success', 'message': 'Expense successfully added!'})

        except Exception as e:
            connection.rollback()
            print(f"Error in add_expense route: {str(e)}")
            return jsonify({'status': 'error', 'message': 'Error in adding expense'})

        finally:
            connection.close()
    else:
        return jsonify({'status': 'error', 'message': 'Error: No database connection'})


async def fetch_order_details():
    # Run the function in the background
    global order_details
    order_details = await getShopifyOrders()


@app.route('/get_payables')
def get_payables():
    connection = check_database_connection()

    if connection:
        try:
            cursor = connection.cursor()

            # Fetching data from payables table
            cursor.execute("""SELECT vendor, amount, pendingsince FROM payables""")
            rows = cursor.fetchall()  # Correctly fetch the rows from the cursor

            payables_list = []
            total_payables = 0  # Initialize total payables

            for payable in rows:
                vendor, amount, pending_since = payable
                pending_days = (datetime.now().date() - pending_since).days

                # Calculate total payables
                total_payables += amount

                # Prepare payables data to return
                payables_list.append({
                    'vendor': vendor,
                    'amount': f"Rs {amount}",
                    'pending_since': pending_since.strftime('%d %b %Y'),
                    'pending_days': pending_days
                })
            print(total_payables)

            # Return both the payables list and the total payables
            return jsonify({
                'payables': payables_list,
                'total_payables': f"Rs {total_payables}"
            })

        except Exception as e:
            connection.rollback()
            print(f"Error in get_payables route: {str(e)}")
            return jsonify({'status': 'error', 'message': 'Error in fetching payables'})

        finally:
            connection.close()
    else:
        return jsonify({'status': 'error', 'message': 'Error: No database connection'})


@app.route('/mark_paid', methods=['POST'])
def mark_paid():
    vendor = request.args.get('vendor')

    if not vendor:
        return jsonify({'status': 'error', 'message': 'Vendor parameter is missing'})
    print(vendor)
    connection = check_database_connection()
    if connection:
        try:
            cursor = connection.cursor()

            # Use named parameters for SQL Server
            cursor.execute("DELETE FROM payables WHERE vendor = %s", (vendor))

            connection.commit()
            return jsonify({'status': 'success', 'message': 'Payable marked as paid'})

        except Exception as e:
            connection.rollback()
            print(f"Error in mark_paid route: {str(e)}")
            return jsonify({'status': 'error', 'message': 'Error in marking payable as paid'})

        finally:
            connection.close()
    else:
        return jsonify({'status': 'error', 'message': 'Error: No database connection'})


@app.route('/update_amount', methods=['POST'])
def update_amount():
    try:
        data = request.get_json()
        vendor = data.get('vendor')
        new_amount = data.get('amount')
        print(f"{vendor} : {new_amount}")

        if not vendor or not new_amount:
            return jsonify({'status': 'error', 'message': 'Vendor or amount is missing'})

        connection = check_database_connection()
        if connection:
            try:
                cursor = connection.cursor()

                # Use parameterized queries to prevent SQL injection
                cursor.execute("UPDATE payables SET amount = %s WHERE vendor = %s", (new_amount, vendor))

                connection.commit()
                return jsonify({'status': 'success', 'message': 'Amount updated successfully'})

            except Exception as e:
                connection.rollback()
                print(f"Error in update_amount route: {str(e)}")
                return jsonify({'status': 'error', 'message': 'Error updating amount'})

            finally:
                connection.close()
        else:
            return jsonify({'status': 'error', 'message': 'Error: No database connection'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error: {str(e)}'})


@app.route('/add_payable', methods=['POST'])
def add_payable():
    try:
        data = request.get_json()
        vendor = data.get('vendor')
        amount = data.get('amount')
        pending_since = data.get('pending_since')

        if not vendor or not amount or not pending_since:
            return jsonify({'status': 'error', 'message': 'All fields are required'})

        connection = check_database_connection()
        if connection:
            try:
                cursor = connection.cursor()

                # Insert new payable into the database
                cursor.execute("""
                    INSERT INTO payables (vendor, amount, pendingsince)
                    VALUES (%s, %s, %s)
                """, (vendor, amount, pending_since))

                connection.commit()
                return jsonify({'status': 'success', 'message': 'Payable added successfully'})

            except Exception as e:
                connection.rollback()
                print(f"Error in add_payable route: {str(e)}")
                return jsonify({'status': 'error', 'message': 'Error adding payable'})

            finally:
                connection.close()
        else:
            return jsonify({'status': 'error', 'message': 'Error: No database connection'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error: {str(e)}'})


@app.route('/pending')
def pending_orders():
    all_orders = []
    pending_items_dict = {}  # Dictionary to track quantities of each unique item

    global daraz_orders, order_details

    # Process Daraz orders with the specified statuses
    for daraz_order in daraz_orders:
        if daraz_order['status'] in ['Ready To Ship', 'Pending']:
            daraz_order_data = {
                'order_via': 'Daraz',
                'order_id': daraz_order['order_id'],
                'status': daraz_order['status'],
                'tracking_number': daraz_order['items_list'][0]['tracking_number'],
                'date': daraz_order['date'],
                'items_list': daraz_order['items_list']
            }
            all_orders.append(daraz_order_data)

            for item in daraz_order['items_list']:
                product_title = item['item_title']
                quantity = item['quantity']
                item_image = item['item_image']

                if product_title in pending_items_dict:
                    pending_items_dict[product_title]['quantity'] += quantity
                else:
                    pending_items_dict[product_title] = {
                        'item_image': item_image,
                        'item_title': product_title,
                        'quantity': quantity
                    }

    # Process Shopify orders with the specified statuses
    for shopify_order in order_details:
        if shopify_order['status'] in ['Booked', 'Un-Booked']:
            shopify_items_list = [
                {
                    'item_image': item['image_src'],
                    'item_title': item['product_title'],
                    'quantity': item['quantity'],
                    'tracking_number': item['tracking_number'],
                    'status': item['status']
                }
                for item in shopify_order['line_items']
            ]

            shopify_order_data = {
                'order_via': 'Shopify',
                'order_id': shopify_order['order_id'],
                'status': shopify_order['status'],
                'tracking_number': shopify_order['tracking_id'],
                'date': shopify_order['created_at'],
                'items_list': shopify_items_list
            }
            all_orders.append(shopify_order_data)

            # Count quantities for each item in the Shopify order
            for item in shopify_items_list:
                product_title = item['item_title']
                quantity = item['quantity']
                item_image = item['item_image']

                if product_title in pending_items_dict:
                    pending_items_dict[product_title]['quantity'] += quantity
                else:
                    pending_items_dict[product_title] = {
                        'item_image': item_image,
                        'item_title': product_title,
                        'quantity': quantity
                    }

    pending_items = list(pending_items_dict.values())
    pending_items_sorted = sorted(pending_items, key=lambda x: x['quantity'], reverse=True)

    # Split the list into two halves
    half = len(pending_items_sorted) // 2

    return render_template('pending.html', all_orders=all_orders, pending_items=pending_items_sorted, half=half)


@app.route('/undelivered')
def undelivered():
    global order_details, pre_loaded, daraz_orders
    return render_template("undelivered.html", order_details=order_details, darazOrders=daraz_orders)


shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)
statuses=['shipped', 'pending', 'ready_to_ship']
daraz_orders = get_daraz_orders(statuses)
order_details = asyncio.run(getShopifyOrders())

if __name__ == "__main__":
    shop_url = os.getenv('SHOP_URL')
    api_key = os.getenv('API_KEY')
    password = os.getenv('PASSWORD')
    app.run(port=5001)
