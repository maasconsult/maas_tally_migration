# Copyright (c) 2026, Greycube Technologies and contributors
# For license information, please see license.txt

import json
import re
import sys
import traceback
import zipfile
from decimal import Decimal


import frappe
from bs4 import BeautifulSoup as bs
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import (
	create_custom_fields as _create_custom_fields,
)
from frappe.model.document import Document
from frappe.utils.data import add_days, add_years, format_datetime, getdate, now

from erpnext import encode_company_abbr
from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import create_charts
from erpnext.accounts.doctype.chart_of_accounts_importer.chart_of_accounts_importer import (
	unset_existing_data,
)

PRIMARY_ACCOUNT = "Primary"
VOUCHER_CHUNK_SIZE = 500


@frappe.whitelist()
def new_doc(document):
	document = json.loads(document)
	doctype = document.pop("doctype")
	document.pop("name", None)
	doc = frappe.new_doc(doctype)
	doc.update(document)

	return doc


class TallyMigration(Document):
	def validate(self):
		failed_import_log = json.loads(self.failed_import_log or "[]")

		def get_creation(row):
			doc = row.get("doc") or {}
			return doc.get("creation") or doc.get("modified") or ""

		sorted_failed_import_log = sorted(failed_import_log, key=get_creation)
		self.failed_import_log = json.dumps(sorted_failed_import_log)

	def autoname(self):
		if not self.name:
			self.name = "Tally Migration on " + format_datetime(self.creation)


	def get_collection(self, data_file):
		def decode_content(encoded_content):
			if isinstance(encoded_content, str):
				return encoded_content

			encodings = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-8")
			for encoding in encodings:
				try:
					return encoded_content.decode(encoding)
				except UnicodeDecodeError:
					pass

			return encoded_content.decode("utf-8", errors="ignore")

		def sanitize_xml(content):
			content = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", content)

			def repl(match):
				code = int(match.group(1))
				if code in (9, 10, 13):
					return match.group(0)
				return ""

			content = re.sub(r"&#(\d+);", repl, content)
			content = content.replace("&#4;", "")
			return content

		def get_child_text(node, tag_name):
			if node is None:
				return None

			child = node.find(tag_name)
			if child is None:
				return None

			value = child.get_text(" ", strip=True)
			if value:
				return value

			return None

		master_file = frappe.get_doc("File", {"file_url": data_file})
		master_file_path = master_file.get_full_path()

		if zipfile.is_zipfile(master_file_path):
			with zipfile.ZipFile(master_file_path) as zf:
				encoded_content = zf.read(zf.namelist()[0])
				content = decode_content(encoded_content)
		else:
			with open(master_file_path, "rb") as f:
				content = decode_content(f.read())

		content = sanitize_xml(content)
		master = bs(content, "xml")

		body = master.find("BODY")
		import_data = body.find("IMPORTDATA") if body else master.find("IMPORTDATA")
		request_data = import_data.find("REQUESTDATA") if import_data else master.find("REQUESTDATA")

		if request_data is None:
			frappe.throw(_("Could not find BODY > IMPORTDATA > REQUESTDATA in the uploaded Tally XML."))

		request_desc = import_data.find("REQUESTDESC") if import_data else master.find("REQUESTDESC")
		company_name = get_child_text(request_desc, "SVCURRENTCOMPANY")

		if company_name and request_data.find("REMOTECMPINFO.LIST") is None:
			tally_message = master.new_tag("TALLYMESSAGE")
			remote_info = master.new_tag("REMOTECMPINFO.LIST")
			remote_name = master.new_tag("REMOTECMPNAME")
			remote_name.string = company_name
			remote_info.append(remote_name)
			tally_message.append(remote_info)
			request_data.insert(0, tally_message)

		return request_data


	def dump_processed_data(self, data):
		safe_docname = re.sub(r"[^A-Za-z0-9_-]+", "_", self.name or "tally_migration")

		for index, row in enumerate(data.items()):
			key, value = row

			content = json.dumps(value, default=str, indent=2)

			# Frappe can deduplicate identical file content.
			# Empty JSON lists like [] can otherwise point to the same File URL.
			# Extra whitespace keeps valid JSON but gives each processed file unique content.
			content = content + "\n" + (" " * (index + 1))

			f = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": safe_docname + "_" + key + ".json",
					"attached_to_doctype": self.doctype,
					"attached_to_name": self.name,
					"content": content,
					"is_private": True,
				}
			)

			f.insert(ignore_permissions=True)
			setattr(self, key, f.file_url)

	def set_account_defaults(self):
		self.default_cost_center, self.default_round_off_account = frappe.db.get_value(
			"Company", self.erpnext_company, ["cost_center", "round_off_account"]
		)
		self.default_warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")


	def _process_master_data(self):
		def get_tag_text(node, tag_name, default=None):
			if node is None:
				return default

			child = node.find(tag_name)
			if child is None:
				return default

			value = child.get_text(" ", strip=True)
			if value:
				return value

			return default

		def get_attr_text(node, attr, default=None):
			if node is None:
				return default

			value = node.get(attr)
			if value is None:
				return default

			value = str(value).strip()
			if value:
				return value

			return default

		def get_master_name(node):
			return (
				get_attr_text(node, "NAME")
				or get_tag_text(node, "NAME")
				or get_tag_text(node, "ORIGINALNAME")
			)

		def clean_number(value):
			if value is None:
				return None

			value = str(value).strip()
			value = value.replace(",", "")
			if value == "":
				return None

			return value

		def is_zero_like(value):
			value = clean_number(value)
			if value in (None, "", "0", "0.0", "0.00", "-0", "-0.0", "-0.00"):
				return True

			return False

		def get_company_name(collection):
			remote_company = collection.find("REMOTECMPINFO.LIST")
			company_name = get_tag_text(remote_company, "REMOTECMPNAME")

			if company_name:
				return company_name

			company_name = get_tag_text(collection, "SVCURRENTCOMPANY")
			if company_name:
				return company_name

			if self.erpnext_company:
				return self.erpnext_company

			if self.tally_company:
				return self.tally_company

			frappe.throw(
				_("Could not determine company name from Tally XML. Expected REMOTECMPNAME or SVCURRENTCOMPANY.")
			)

		def get_coa_customers_suppliers(collection):
			root_type_map = {
				"Application of Funds (Assets)": "Asset",
				"Expenses": "Expense",
				"Income": "Income",
				"Source of Funds (Liabilities)": "Liability",
			}
			roots = set(root_type_map.keys())
			accounts = list(get_groups(collection.find_all("GROUP"))) + list(
				get_ledgers(collection.find_all("LEDGER"))
			)
			children, parents = get_children_and_parent_dict(accounts)
			group_set = [acc[1] for acc in accounts if acc[2]]
			children, customers, suppliers = remove_parties(parents, children, group_set)

			try:
				coa = traverse({}, children, roots, roots, group_set)
			except RecursionError:
				self.log(
					_(
						"Error occured while parsing Chart of Accounts: Please make sure that no two accounts have the same name"
					)
				)
				coa = {}

			for account in coa:
				coa[account]["root_type"] = root_type_map[account]

			return coa, customers, suppliers

		def get_groups(accounts):
			for account in accounts:
				name = get_master_name(account)
				if not name:
					continue

				if name in (self.tally_creditors_account, self.tally_debtors_account):
					yield get_parent(account), name, 0
				else:
					yield get_parent(account), name, 1

		def get_ledgers(accounts):
			for account in accounts:
				name = get_master_name(account)
				parent = get_tag_text(account, "PARENT")

				if parent and name:
					yield parent, name, 0

		def get_parent(account):
			parent = get_tag_text(account, "PARENT")
			if parent:
				return parent

			is_deemed_positive = get_tag_text(account, "ISDEEMEDPOSITIVE")
			is_revenue = get_tag_text(account, "ISREVENUE")

			root_map = {
				("Yes", "No"): "Application of Funds (Assets)",
				("Yes", "Yes"): "Expenses",
				("No", "Yes"): "Income",
				("No", "No"): "Source of Funds (Liabilities)",
			}

			return root_map.get((is_deemed_positive, is_revenue), "Application of Funds (Assets)")

		def get_children_and_parent_dict(accounts):
			children, parents = {}, {}
			for parent, account, _is_group in accounts:
				if not parent or not account:
					continue

				if parent == account:
					continue

				children.setdefault(parent, set()).add(account)
				parents.setdefault(account, set()).add(parent)

			return children, parents


		def get_all_ancestors(account, parents, visited=None):
			if visited is None:
				visited = set()

			ancestors = set()

			for parent in parents.get(account, set()):
				if parent in visited:
					continue

				visited.add(parent)
				ancestors.add(parent)
				ancestors.update(get_all_ancestors(parent, parents, visited))

			return ancestors

		def remove_parties(parents, children, group_set):
			customers, suppliers = set(), set()

			for account in parents:
				if account in group_set:
					continue

				ancestors = get_all_ancestors(account, parents)

				found = False

				if self.tally_creditors_account in ancestors:
					suppliers.add(account)
					found = True

				if self.tally_debtors_account in ancestors:
					customers.add(account)
					found = True

				if found:
					children.pop(account, None)

			return children, customers, suppliers

		def traverse(tree, children, accounts, roots, group_set, visited=None):
			if visited is None:
				visited = set()

			for account in accounts:
				if account in visited:
					continue

				visited.add(account)

				if account in group_set or account in roots:
					if account in children:
						tree[account] = traverse(
							{},
							children,
							children[account],
							roots,
							group_set,
							visited.copy(),
						)
					else:
						tree[account] = {"is_group": 1}
				else:
					tree[account] = {}

			return tree


		def get_group_maps(collection):
			group_parent = {}

			for group in collection.find_all("GROUP"):
				group_name = get_master_name(group)
				parent_name = get_tag_text(group, "PARENT")

				if group_name:
					group_parent[group_name] = parent_name

			return group_parent

		def group_has_ancestor(group_name, target_group_name, group_parent):
			visited = set()
			current = group_name

			while current:
				if current in visited:
					return False

				visited.add(current)

				if current == target_group_name:
					return True

				current = group_parent.get(current)

			return False

		def get_party_group_for_ledger(ledger_parent, root_group_name, group_parent, default_group):
			if not ledger_parent:
				return default_group

			if ledger_parent == root_group_name:
				return root_group_name

			if group_has_ancestor(ledger_parent, root_group_name, group_parent):
				return ledger_parent

			return default_group

		def get_party_groups(collection, customers, suppliers):
			group_parent = get_group_maps(collection)

			customer_group_rows = []
			supplier_group_rows = []

			customer_group_names = set()
			supplier_group_names = set()

			def add_customer_group(group_name):
				if not group_name or group_name in customer_group_names:
					return

				parent_name = group_parent.get(group_name)

				if not parent_name or parent_name == "Primary":
					parent_customer_group = "All Customer Groups"
				elif parent_name == self.tally_debtors_account:
					parent_customer_group = self.tally_debtors_account
				elif group_has_ancestor(parent_name, self.tally_debtors_account, group_parent):
					parent_customer_group = parent_name
				else:
					parent_customer_group = "All Customer Groups"

				customer_group_names.add(group_name)
				customer_group_rows.append(
					{
						"doctype": "Customer Group",
						"customer_group_name": group_name,
						"parent_customer_group": parent_customer_group,
						"is_group": 1,
					}
				)

			def add_supplier_group(group_name):
				if not group_name or group_name in supplier_group_names:
					return

				parent_name = group_parent.get(group_name)

				if not parent_name or parent_name == "Primary":
					parent_supplier_group = "All Supplier Groups"
				elif parent_name == self.tally_creditors_account:
					parent_supplier_group = self.tally_creditors_account
				elif group_has_ancestor(parent_name, self.tally_creditors_account, group_parent):
					parent_supplier_group = parent_name
				else:
					parent_supplier_group = "All Supplier Groups"

				supplier_group_names.add(group_name)
				supplier_group_rows.append(
					{
						"doctype": "Supplier Group",
						"supplier_group_name": group_name,
						"parent_supplier_group": parent_supplier_group,
						"is_group": 1,
					}
				)

			add_customer_group(self.tally_debtors_account)
			add_supplier_group(self.tally_creditors_account)

			for group_name in group_parent:
				if group_name == self.tally_debtors_account or group_has_ancestor(group_name, self.tally_debtors_account, group_parent):
					add_customer_group(group_name)

				if group_name == self.tally_creditors_account or group_has_ancestor(group_name, self.tally_creditors_account, group_parent):
					add_supplier_group(group_name)

			return customer_group_rows, supplier_group_rows

		def get_parties_addresses(collection, customers, suppliers, customer_groups=None, supplier_groups=None):
			parties, addresses = [], []
			group_parent = get_group_maps(collection)

			customer_group_names = set()
			supplier_group_names = set()

			for row in customer_groups or []:
				group_name = row.get("customer_group_name")
				if group_name:
					customer_group_names.add(group_name)

			for row in supplier_groups or []:
				group_name = row.get("supplier_group_name")
				if group_name:
					supplier_group_names.add(group_name)

			for account in collection.find_all("LEDGER"):
				ledger_name = get_master_name(account)
				ledger_parent = get_tag_text(account, "PARENT")

				if not ledger_name:
					continue

				party_type = None
				links = []

				if ledger_name in customers:
					party_type = "Customer"
					customer_group = get_party_group_for_ledger(
						ledger_parent,
						self.tally_debtors_account,
						group_parent,
						"All Customer Groups",
					)

					if customer_group not in customer_group_names:
						customer_group = "All Customer Groups"

					parties.append(
						{
							"doctype": party_type,
							"customer_name": ledger_name,
							"tax_id": get_tag_text(account, "INCOMETAXNUMBER"),
							"customer_group": customer_group,
							"territory": "All Territories",
							"customer_type": "Individual",
						}
					)
					links.append({"link_doctype": party_type, "link_name": ledger_name})

				if ledger_name in suppliers:
					party_type = "Supplier"
					supplier_group = get_party_group_for_ledger(
						ledger_parent,
						self.tally_creditors_account,
						group_parent,
						"All Supplier Groups",
					)

					if supplier_group not in supplier_group_names:
						supplier_group = "All Supplier Groups"

					parties.append(
						{
							"doctype": party_type,
							"supplier_name": ledger_name,
							"pan": get_tag_text(account, "INCOMETAXNUMBER"),
							"supplier_group": supplier_group,
							"supplier_type": "Individual",
						}
					)
					links.append({"link_doctype": party_type, "link_name": ledger_name})

				if party_type:
					address = "\n".join(
						[a.get_text(" ", strip=True) for a in account.find_all("ADDRESS") if a.get_text(" ", strip=True)]
					)
					addresses.append(
						{
							"doctype": "Address",
							"address_line1": address[:140].strip(),
							"address_line2": address[140:].strip(),
							"country": get_tag_text(account, "COUNTRYNAME"),
							"state": get_tag_text(account, "LEDSTATENAME"),
							"gst_state": get_tag_text(account, "LEDSTATENAME"),
							"pin_code": get_tag_text(account, "PINCODE"),
							"mobile": get_tag_text(account, "LEDGERPHONE"),
							"phone": get_tag_text(account, "LEDGERPHONE"),
							"gstin": get_tag_text(account, "PARTYGSTIN"),
							"links": links,
						}
					)

			return parties, addresses

		def get_stock_items_uoms(collection):
			uoms = []
			seen_uoms = set()

			for uom in collection.find_all("UNIT"):
				uom_name = get_master_name(uom)
				if not uom_name or uom_name in seen_uoms:
					continue

				seen_uoms.add(uom_name)
				uoms.append({"doctype": "UOM", "uom_name": uom_name})

			items = []
			for item in collection.find_all("STOCKITEM"):
				item_name = get_master_name(item)
				if not item_name:
					continue

				stock_uom = get_tag_text(item, "BASEUNITS") or self.default_uom
				items.append(
					{
						"doctype": "Item",
						"item_code": item_name,
						"stock_uom": stock_uom.strip(),
						"is_stock_item": 0,
						"item_group": "All Item Groups",
						"item_defaults": [{"company": self.erpnext_company}],
					}
				)

			return items, uoms


		def get_currencies(collection):
			rows = []
			seen = set()

			for currency in collection.find_all("CURRENCY"):
				tally_name = get_master_name(currency)
				original_name = get_tag_text(currency, "ORIGINALNAME")
				formal_name = get_tag_text(currency, "FORMALNAME")
				symbol = get_tag_text(currency, "SYMBOL")
				decimal_places = get_tag_text(currency, "DECIMALPLACES")
				decimal_symbol = get_tag_text(currency, "DECIMALSYMBOL")

				search_text = " ".join(
					[
						str(tally_name or ""),
						str(original_name or ""),
						str(formal_name or ""),
						str(symbol or ""),
						str(decimal_symbol or ""),
					]
				).lower()

				if "halala" in search_text or "riyal" in search_text or "riyals" in search_text:
					currency_code = "SAR"
					currency_name = "Saudi Riyal"
					currency_symbol = symbol or "ر.س"
				else:
					currency_code = None

					for candidate in (original_name, formal_name, tally_name):
						candidate = str(candidate or "").strip()
						if len(candidate) == 3 and candidate.isalpha():
							currency_code = candidate.upper()
							break

					if not currency_code:
						currency_code = str(tally_name or original_name or formal_name or "").strip()

					currency_name = formal_name or original_name or currency_code
					currency_symbol = symbol or tally_name

				if not currency_code:
					continue

				if currency_code in seen:
					continue

				seen.add(currency_code)

				rows.append(
					{
						"doctype": "Currency",
						"currency": currency_code,
						"name": currency_code,
						"currency_name": currency_name,
						"symbol": currency_symbol,
						"tally_name": tally_name,
						"original_name": original_name,
						"formal_name": formal_name,
						"decimal_places": decimal_places,
						"decimal_symbol": decimal_symbol,
					}
				)

			return rows

		def get_cost_categories(collection):
			rows = []

			for category in collection.find_all("COSTCATEGORY"):
				name = get_master_name(category)
				if not name:
					continue

				rows.append(
					{
						"tally_master_type": "COSTCATEGORY",
						"name": name,
						"allocate_revenue_items": get_tag_text(category, "ALLOCATEREVENUE"),
						"allocate_non_revenue_items": get_tag_text(category, "ALLOCATENONREVENUE"),
					}
				)

			return rows

		def get_cost_centers(collection):
			rows = []
			cost_centers = collection.find_all("COSTCENTRE") + collection.find_all("COSTCENTER")

			for cost_center in cost_centers:
				name = get_master_name(cost_center)
				if not name:
					continue

				rows.append(
					{
						"doctype": "Cost Center",
						"cost_center_name": name,
						"company": self.erpnext_company,
						"parent_cost_center": get_tag_text(cost_center, "PARENT"),
						"category": get_tag_text(cost_center, "CATEGORY"),
						"is_group": 1 if get_tag_text(cost_center, "ISCOSTCENTREGROUP") == "Yes" else 0,
					}
				)

			return rows

		def get_item_groups(collection):
			rows = []

			for stock_group in collection.find_all("STOCKGROUP"):
				name = get_master_name(stock_group)
				if not name:
					continue

				rows.append(
					{
						"doctype": "Item Group",
						"item_group_name": name,
						"parent_item_group": get_tag_text(stock_group, "PARENT") or "All Item Groups",
						"is_group": 1,
						"tally_name": name,
					}
				)

			return rows

		def get_warehouses(collection):
			rows = []

			for godown in collection.find_all("GODOWN"):
				name = get_master_name(godown)
				if not name:
					continue

				rows.append(
					{
						"doctype": "Warehouse",
						"warehouse_name": name,
						"company": self.erpnext_company,
						"parent_warehouse": get_tag_text(godown, "PARENT"),
						"tally_name": name,
					}
				)

			return rows

		def get_employee_groups(collection):
			rows = []

			for group in collection.find_all("EMPLOYEEGROUP") + collection.find_all("EMPLOYEECATEGORY"):
				name = get_master_name(group)
				if not name:
					continue

				rows.append(
					{
						"tally_master_type": group.name,
						"name": name,
						"parent": get_tag_text(group, "PARENT"),
					}
				)

			return rows

		def get_employees(collection):
			rows = []

			for employee in collection.find_all("EMPLOYEE"):
				name = get_master_name(employee)
				if not name:
					continue

				rows.append(
					{
						"doctype": "Employee",
						"employee_name": name,
						"employee_number": get_tag_text(employee, "EMPLOYEENUMBER"),
						"company": self.erpnext_company,
						"parent": get_tag_text(employee, "PARENT"),
						"designation": get_tag_text(employee, "DESIGNATION"),
						"department": get_tag_text(employee, "DEPARTMENT"),
						"date_of_joining": get_tag_text(employee, "DATEOFJOINING"),
						"date_of_birth": get_tag_text(employee, "DATEOFBIRTH"),
						"gender": get_tag_text(employee, "GENDER"),
						"pan_number": get_tag_text(employee, "INCOMETAXNUMBER"),
					}
				)

			return rows

		def get_payheads(collection):
			rows = []

			for payhead in collection.find_all("PAYHEAD"):
				name = get_master_name(payhead)
				if not name:
					continue

				rows.append(
					{
						"doctype": "Salary Component",
						"salary_component": name,
						"salary_component_abbr": name[:20],
						"tally_name": name,
						"parent": get_tag_text(payhead, "PARENT"),
						"payhead_type": get_tag_text(payhead, "PAYHEADTYPE"),
						"income_type": get_tag_text(payhead, "INCOMETYPE"),
						"calculation_type": get_tag_text(payhead, "CALCULATIONTYPE"),
						"attendance_type": get_tag_text(payhead, "ATTENDANCETYPE"),
						"under": get_tag_text(payhead, "PARENT"),
					}
				)

			return rows

		def get_ledger_opening_balances(collection, customers, suppliers):
			rows = []

			for ledger in collection.find_all("LEDGER"):
				name = get_master_name(ledger)
				opening_balance = clean_number(get_tag_text(ledger, "OPENINGBALANCE"))

				if not name or is_zero_like(opening_balance):
					continue

				party_type = None
				if name in customers:
					party_type = "Customer"
				elif name in suppliers:
					party_type = "Supplier"

				rows.append(
					{
						"ledger_name": name,
						"parent": get_tag_text(ledger, "PARENT"),
						"opening_balance": opening_balance,
						"party_type": party_type,
						"company": self.erpnext_company,
					}
				)

			return rows

		def get_stock_items_enhanced(collection):
			rows = []

			for item in collection.find_all("STOCKITEM"):
				name = get_master_name(item)
				if not name:
					continue

				rows.append(
					{
						"doctype": "Item",
						"item_code": name,
						"item_name": name,
						"stock_uom": get_tag_text(item, "BASEUNITS") or self.default_uom,
						"stock_group": get_tag_text(item, "PARENT"),
						"opening_balance": clean_number(get_tag_text(item, "OPENINGBALANCE")),
						"opening_value": clean_number(get_tag_text(item, "OPENINGVALUE")),
						"opening_rate": clean_number(get_tag_text(item, "OPENINGRATE")),
						"gst_applicable": get_tag_text(item, "GSTAPPLICABLE"),
						"hsn_code": get_tag_text(item, "HSNCODE"),
						"taxability": get_tag_text(item, "TAXABILITY"),
						"company": self.erpnext_company,
					}
				)

			return rows

		def get_item_opening_balances(collection):
			rows = []

			for item in collection.find_all("STOCKITEM"):
				item_name = get_master_name(item)
				opening_balance = clean_number(get_tag_text(item, "OPENINGBALANCE"))
				opening_value = clean_number(get_tag_text(item, "OPENINGVALUE"))
				opening_rate = clean_number(get_tag_text(item, "OPENINGRATE"))

				if not item_name:
					continue

				if is_zero_like(opening_balance) and is_zero_like(opening_value):
					continue

				allocations = []

				for allocation in item.find_all("BATCHALLOCATIONS.LIST") + item.find_all("GODOWNALLOCATIONS.LIST"):
					allocations.append(
						{
							"godown": get_tag_text(allocation, "GODOWNNAME"),
							"batch": get_tag_text(allocation, "BATCHNAME"),
							"quantity": clean_number(get_tag_text(allocation, "OPENINGBALANCE")),
							"value": clean_number(get_tag_text(allocation, "OPENINGVALUE")),
							"rate": clean_number(get_tag_text(allocation, "OPENINGRATE")),
						}
					)

				rows.append(
					{
						"item_code": item_name,
						"stock_uom": get_tag_text(item, "BASEUNITS") or self.default_uom,
						"stock_group": get_tag_text(item, "PARENT"),
						"opening_balance": opening_balance,
						"opening_value": opening_value,
						"opening_rate": opening_rate,
						"allocations": allocations,
					}
				)

			return rows

		def parse_tally_date(value):
			value = (value or "").strip()

			if len(value) == 8 and value.isdigit():
				return value[:4] + "-" + value[4:6] + "-" + value[6:8]

			return None

		def number_value(value):
			try:
				return float(clean_number(value) or 0)
			except Exception:
				try:
					return float(str(value or "0").replace(",", "").strip())
				except Exception:
					return 0.0

		def parse_tally_qty(value):
			value = (value or "").strip()

			if not value:
				return 0.0, self.default_uom or "Unit"

			parts = value.split()

			if not parts:
				return 0.0, self.default_uom or "Unit"

			qty = number_value(parts[0])
			uom = " ".join(parts[1:]).strip() or self.default_uom or "Unit"

			return qty, uom

		def parse_tally_rate(value):
			value = (value or "").strip()

			if not value:
				return 0.0, None

			if "/" in value:
				rate, uom = value.split("/", 1)
				return abs(number_value(rate)), uom.strip()

			return abs(number_value(value)), None

		def get_opening_party_invoices(collection, customers, suppliers):
			sales_invoices = []
			purchase_invoices = []

			for ledger in collection.find_all("LEDGER"):
				ledger_name = get_master_name(ledger)

				if not ledger_name:
					continue

				if ledger_name not in customers and ledger_name not in suppliers:
					continue

				ledger_parent = get_tag_text(ledger, "PARENT")
				ledger_opening_balance = number_value(get_tag_text(ledger, "OPENINGBALANCE"))

				for idx, bill in enumerate(ledger.find_all("BILLALLOCATIONS.LIST"), start=1):
					bill_name = get_tag_text(bill, "NAME") or (ledger_name + "-Opening-" + str(idx))
					bill_date = parse_tally_date(get_tag_text(bill, "BILLDATE"))
					opening_balance = number_value(get_tag_text(bill, "OPENINGBALANCE") or get_tag_text(bill, "AMOUNT"))
					is_advance = get_tag_text(bill, "ISADVANCE")

					if not opening_balance:
						continue

					row = {
						"party": ledger_name,
						"party_ledger": ledger_name,
						"party_parent": ledger_parent,
						"invoice_number": bill_name,
						"posting_date": bill_date,
						"due_date": bill_date,
						"outstanding_amount": abs(opening_balance),
						"tally_opening_balance": opening_balance,
						"ledger_opening_balance": ledger_opening_balance,
						"is_advance": 1 if str(is_advance).strip().lower() == "yes" else 0,
					}

					if ledger_name in customers:
						row.update(
							{
								"doctype": "Sales Invoice",
								"invoice_type": "Sales",
								"party_type": "Customer",
								"customer": ledger_name,
							}
						)
						sales_invoices.append(row)

					if ledger_name in suppliers:
						row.update(
							{
								"doctype": "Purchase Invoice",
								"invoice_type": "Purchase",
								"party_type": "Supplier",
								"supplier": ledger_name,
							}
						)
						purchase_invoices.append(row)

			return sales_invoices, purchase_invoices

		def get_opening_stock_items(collection):
			opening_stock_items = []

			for item in collection.find_all("STOCKITEM"):
				item_name = get_master_name(item)

				if not item_name:
					continue

				item_opening_balance = get_tag_text(item, "OPENINGBALANCE")
				item_opening_value = get_tag_text(item, "OPENINGVALUE")
				item_opening_rate = get_tag_text(item, "OPENINGRATE")

				batches = item.find_all("BATCHALLOCATIONS.LIST")

				if batches:
					for batch in batches:
						qty, qty_uom = parse_tally_qty(get_tag_text(batch, "OPENINGBALANCE"))
						opening_value = number_value(get_tag_text(batch, "OPENINGVALUE"))
						valuation_rate, rate_uom = parse_tally_rate(get_tag_text(batch, "OPENINGRATE"))

						if not qty:
							continue

						if not valuation_rate and qty:
							valuation_rate = abs(opening_value) / abs(qty)

						opening_stock_items.append(
							{
								"doctype": "Stock Reconciliation Item",
								"item_code": item_name,
								"item_name": item_name,
								"warehouse_name": get_tag_text(batch, "GODOWNNAME"),
								"batch_name": get_tag_text(batch, "BATCHNAME"),
								"manufacturing_date": parse_tally_date(get_tag_text(batch, "MFDON")),
								"qty": abs(qty),
								"uom": qty_uom or rate_uom or self.default_uom or "Unit",
								"valuation_rate": abs(valuation_rate),
								"opening_value": abs(opening_value),
								"tally_opening_balance": get_tag_text(batch, "OPENINGBALANCE"),
								"tally_opening_value": get_tag_text(batch, "OPENINGVALUE"),
								"tally_opening_rate": get_tag_text(batch, "OPENINGRATE"),
								"source": "BATCHALLOCATIONS.LIST",
							}
						)

					continue

				qty, qty_uom = parse_tally_qty(item_opening_balance)
				opening_value = number_value(item_opening_value)
				valuation_rate, rate_uom = parse_tally_rate(item_opening_rate)

				if not qty:
					continue

				if not valuation_rate and qty:
					valuation_rate = abs(opening_value) / abs(qty)

				opening_stock_items.append(
					{
						"doctype": "Stock Reconciliation Item",
						"item_code": item_name,
						"item_name": item_name,
						"warehouse_name": None,
						"batch_name": None,
						"manufacturing_date": None,
						"qty": abs(qty),
						"uom": qty_uom or rate_uom or self.default_uom or "Unit",
						"valuation_rate": abs(valuation_rate),
						"opening_value": abs(opening_value),
						"tally_opening_balance": item_opening_balance,
						"tally_opening_value": item_opening_value,
						"tally_opening_rate": item_opening_rate,
						"source": "STOCKITEM",
					}
				)

			return opening_stock_items

		try:
			self.publish("Process Master Data", _("Reading Uploaded File"), 1, 7)
			collection = self.get_collection(self.master_data)

			company = get_company_name(collection)
			self.tally_company = company
			self.erpnext_company = company

			self.publish("Process Master Data", _("Processing Chart of Accounts and Parties"), 2, 7)
			chart_of_accounts, customers, suppliers = get_coa_customers_suppliers(collection)

			self.publish("Process Master Data", _("Processing Party Addresses"), 3, 7)
			customer_groups, supplier_groups = get_party_groups(collection, customers, suppliers)
			parties, addresses = get_parties_addresses(collection, customers, suppliers, customer_groups, supplier_groups)

			self.publish("Process Master Data", _("Processing Items and UOMs"), 4, 7)
			items, uoms = get_stock_items_uoms(collection)

			self.publish("Process Master Data", _("Processing Additional Masters"), 5, 7)
			currencies = get_currencies(collection)
			cost_categories = get_cost_categories(collection)
			cost_centers = get_cost_centers(collection)
			item_groups = get_item_groups(collection)
			warehouses = get_warehouses(collection)
			employee_groups = get_employee_groups(collection)
			employees = get_employees(collection)
			payheads = get_payheads(collection)

			self.publish("Process Master Data", _("Processing Opening Balances"), 6, 7)
			ledger_opening_balances = get_ledger_opening_balances(collection, customers, suppliers)
			stock_items_enhanced = get_stock_items_enhanced(collection)
			item_opening_balances = get_item_opening_balances(collection)
			opening_sales_invoices, opening_purchase_invoices = get_opening_party_invoices(collection, customers, suppliers)
			opening_stock_items = get_opening_stock_items(collection)

			data = {
				"chart_of_accounts": chart_of_accounts,
				"customer_groups": customer_groups,
				"supplier_groups": supplier_groups,
				"parties": parties,
				"addresses": addresses,
				"items": items,
				"uoms": uoms,
				"currencies": currencies,
				"cost_categories": cost_categories,
				"cost_centers": cost_centers,
				"item_groups": item_groups,
				"warehouses": warehouses,
				"employee_groups": employee_groups,
				"employees": employees,
				"payheads": payheads,
				"ledger_opening_balances": ledger_opening_balances,
				"stock_items_enhanced": stock_items_enhanced,
				"item_opening_balances": item_opening_balances,
				"opening_sales_invoices": opening_sales_invoices,
				"opening_purchase_invoices": opening_purchase_invoices,
				"opening_stock_items": opening_stock_items,
			}

			self.publish("Process Master Data", _("Done"), 7, 7)
			self.dump_processed_data(data)

			self.is_master_data_processed = 1

		except Exception:
			self.publish("Process Master Data", _("Process Failed"), -1, 7)
			self.log()

		finally:
			self.set_status()

	def publish(self, title, message, count, total):
		frappe.publish_realtime(
			"tally_migration_progress_update",
			{"title": title, "message": message, "count": count, "total": total},
			user=self.modified_by,
		)


	def _import_master_data(self):
		def load_processed_json(file_url, fallback=None):
			if fallback is None:
				fallback = []

			if not file_url:
				return fallback

			try:
				processed_file = frappe.get_doc("File", {"file_url": file_url})
				content = processed_file.get_content()
				if not content:
					return fallback

				return json.loads(content)
			except Exception:
				self.log({"file_url": file_url, "error": "Could not load processed JSON file"})
				return fallback

		def get_company_abbr():
			return frappe.db.get_value("Company", self.erpnext_company, "abbr")

		def get_default_currency():
			currencies = load_processed_json(self.currencies)

			for currency in currencies:
				currency_code = currency.get("currency") or currency.get("name")
				if currency_code:
					return currency_code

			return "INR"

		def get_existing_docname(doctype, filters):
			return frappe.db.get_value(doctype, filters, "name")

		def create_currencies(currencies_file_url):
			currencies = load_processed_json(currencies_file_url)

			for currency in currencies:
				currency_code = currency.get("currency") or currency.get("name")
				if not currency_code:
					continue

				if frappe.db.exists("Currency", currency_code):
					continue

				currency_doc = frappe.get_doc(
					{
						"doctype": "Currency",
						"name": currency_code,
						"currency_name": currency.get("currency_name") or currency_code,
						"symbol": currency.get("symbol") or currency_code,
						"enabled": 1,
					}
				)

				try:
					currency_doc.insert(ignore_permissions=True)
				except frappe.DuplicateEntryError:
					pass
				except Exception:
					self.log(currency_doc)

		def create_company_and_coa(coa_file_url, default_currency):
			coa_file = frappe.get_doc("File", {"file_url": coa_file_url})
			coa = json.loads(coa_file.get_content())

			frappe.local.flags.ignore_chart_of_accounts = True

			try:
				company = frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": self.erpnext_company,
						"default_currency": default_currency,
						"enable_perpetual_inventory": 0,
					}
				).insert(ignore_permissions=True)
			except frappe.DuplicateEntryError:
				company = frappe.get_doc("Company", self.erpnext_company)
				unset_existing_data(self.erpnext_company)

			frappe.local.flags.ignore_chart_of_accounts = False

			create_charts(company.name, custom_chart=coa)
			company.create_default_warehouses()

		def create_item_groups(item_groups_file_url):
			item_groups = load_processed_json(item_groups_file_url)
			pending = []

			for row in item_groups:
				item_group_name = row.get("item_group_name") or row.get("name")
				if not item_group_name:
					continue

				if item_group_name in ("Primary", "All Item Groups"):
					continue

				pending.append(row)

			max_rounds = len(pending) + 5

			for _round in range(max_rounds):
				if not pending:
					break

				still_pending = []

				for row in pending:
					item_group_name = row.get("item_group_name") or row.get("name")
					parent_item_group = row.get("parent_item_group") or "All Item Groups"

					if parent_item_group in ("Primary", item_group_name):
						parent_item_group = "All Item Groups"

					parent_exists = frappe.db.exists("Item Group", parent_item_group)

					if not parent_exists:
						if any((p.get("item_group_name") or p.get("name")) == parent_item_group for p in pending):
							still_pending.append(row)
							continue

						parent_item_group = "All Item Groups"

					if frappe.db.exists("Item Group", item_group_name):
						continue

					item_group_doc = frappe.get_doc(
						{
							"doctype": "Item Group",
							"item_group_name": item_group_name,
							"parent_item_group": parent_item_group,
							"is_group": 1,
						}
					)

					try:
						item_group_doc.insert(ignore_permissions=True)
					except frappe.DuplicateEntryError:
						pass
					except Exception:
						self.log(item_group_doc)

				if len(still_pending) == len(pending):
					for row in still_pending:
						item_group_name = row.get("item_group_name") or row.get("name")

						if not item_group_name or frappe.db.exists("Item Group", item_group_name):
							continue

						item_group_doc = frappe.get_doc(
							{
								"doctype": "Item Group",
								"item_group_name": item_group_name,
								"parent_item_group": "All Item Groups",
								"is_group": 1,
							}
						)

						try:
							item_group_doc.insert(ignore_permissions=True)
						except Exception:
							self.log(item_group_doc)

					break

				pending = still_pending

		def get_root_warehouse():
			company_abbr = get_company_abbr()
			expected_name = "All Warehouses - " + company_abbr

			if frappe.db.exists("Warehouse", expected_name):
				return expected_name

			root = frappe.db.get_value(
				"Warehouse",
				{
					"warehouse_name": "All Warehouses",
					"company": self.erpnext_company,
					"is_group": 1,
				},
				"name",
			)

			return root

		def get_warehouse_name(warehouse_name):
			return frappe.db.get_value(
				"Warehouse",
				{
					"warehouse_name": warehouse_name,
					"company": self.erpnext_company,
				},
				"name",
			)

		def create_warehouses(warehouses_file_url):
			warehouses = load_processed_json(warehouses_file_url)
			root_warehouse = get_root_warehouse()

			if not root_warehouse:
				return

			pending = []

			for row in warehouses:
				warehouse_name = row.get("warehouse_name") or row.get("name")
				if not warehouse_name:
					continue

				if warehouse_name in ("Primary", "All Warehouses"):
					continue

				pending.append(row)

			max_rounds = len(pending) + 5

			for _round in range(max_rounds):
				if not pending:
					break

				still_pending = []

				for row in pending:
					warehouse_name = row.get("warehouse_name") or row.get("name")
					parent_warehouse_name = row.get("parent_warehouse")

					if get_warehouse_name(warehouse_name):
						continue

					parent_warehouse = root_warehouse

					if parent_warehouse_name and parent_warehouse_name not in ("Primary", warehouse_name):
						existing_parent = get_warehouse_name(parent_warehouse_name)

						if existing_parent:
							parent_warehouse = existing_parent
						elif any((p.get("warehouse_name") or p.get("name")) == parent_warehouse_name for p in pending):
							still_pending.append(row)
							continue

					warehouse_doc = frappe.get_doc(
						{
							"doctype": "Warehouse",
							"warehouse_name": warehouse_name,
							"company": self.erpnext_company,
							"parent_warehouse": parent_warehouse,
							"is_group": 0,
						}
					)

					try:
						warehouse_doc.insert(ignore_permissions=True)
					except frappe.DuplicateEntryError:
						pass
					except Exception:
						self.log(warehouse_doc)

				if len(still_pending) == len(pending):
					for row in still_pending:
						warehouse_name = row.get("warehouse_name") or row.get("name")

						if not warehouse_name or get_warehouse_name(warehouse_name):
							continue

						warehouse_doc = frappe.get_doc(
							{
								"doctype": "Warehouse",
								"warehouse_name": warehouse_name,
								"company": self.erpnext_company,
								"parent_warehouse": root_warehouse,
								"is_group": 0,
							}
						)

						try:
							warehouse_doc.insert(ignore_permissions=True)
						except Exception:
							self.log(warehouse_doc)

					break

				pending = still_pending


		def get_cost_center_name(cost_center_name):
			return frappe.db.get_value(
				"Cost Center",
				{
					"cost_center_name": cost_center_name,
					"company": self.erpnext_company,
				},
				"name",
			)

		def get_root_cost_center():
			company_abbr = get_company_abbr()

			if company_abbr:
				expected_name = "All Cost Centers - " + company_abbr
				if frappe.db.exists("Cost Center", expected_name):
					is_group = frappe.db.get_value("Cost Center", expected_name, "is_group")
					if is_group:
						return expected_name

			roots = frappe.get_all(
				"Cost Center",
				filters={
					"company": self.erpnext_company,
					"is_group": 1,
				},
				fields=["name", "lft"],
				order_by="lft asc",
				limit=1,
			)

			if roots:
				return roots[0].name

			default_cost_center = frappe.db.get_value("Company", self.erpnext_company, "cost_center")

			if default_cost_center:
				frappe.db.set_value("Cost Center", default_cost_center, "is_group", 1)
				return default_cost_center

			return None

		def ensure_cost_center_is_group(cost_center_name):
			existing = get_cost_center_name(cost_center_name)
			if existing:
				is_group = frappe.db.get_value("Cost Center", existing, "is_group")
				if not is_group:
					frappe.db.set_value("Cost Center", existing, "is_group", 1)
				return existing

			return None

		def create_cost_centers(cost_centers_file_url):
			cost_centers = load_processed_json(cost_centers_file_url)
			root_cost_center = get_root_cost_center()

			if not root_cost_center:
				self.log({"error": "Could not find or create a root Cost Center group for company", "company": self.erpnext_company})
				return

			parent_names = set()

			for row in cost_centers:
				parent_name = row.get("parent_cost_center")
				cost_center_name = row.get("cost_center_name") or row.get("name")

				if parent_name and parent_name not in ("Primary", cost_center_name):
					parent_names.add(parent_name)

			pending = []

			for row in cost_centers:
				cost_center_name = row.get("cost_center_name") or row.get("name")
				if not cost_center_name:
					continue

				if cost_center_name in ("Primary",):
					continue

				pending.append(row)

			max_rounds = len(pending) + 5

			for _round in range(max_rounds):
				if not pending:
					break

				still_pending = []

				for row in pending:
					cost_center_name = row.get("cost_center_name") or row.get("name")
					parent_cost_center_name = row.get("parent_cost_center")

					should_be_group = 1 if row.get("is_group") or cost_center_name in parent_names else 0

					existing = get_cost_center_name(cost_center_name)
					if existing:
						if should_be_group:
							frappe.db.set_value("Cost Center", existing, "is_group", 1)
						continue

					parent_cost_center = root_cost_center

					if parent_cost_center_name and parent_cost_center_name not in ("Primary", cost_center_name):
						existing_parent = get_cost_center_name(parent_cost_center_name)

						if existing_parent:
							parent_is_group = frappe.db.get_value("Cost Center", existing_parent, "is_group")
							if not parent_is_group:
								frappe.db.set_value("Cost Center", existing_parent, "is_group", 1)
							parent_cost_center = existing_parent

						elif any((p.get("cost_center_name") or p.get("name")) == parent_cost_center_name for p in pending):
							still_pending.append(row)
							continue

					parent_is_group = frappe.db.get_value("Cost Center", parent_cost_center, "is_group")
					if not parent_is_group:
						parent_cost_center = root_cost_center

					cost_center_doc = frappe.get_doc(
						{
							"doctype": "Cost Center",
							"cost_center_name": cost_center_name,
							"company": self.erpnext_company,
							"parent_cost_center": parent_cost_center,
							"is_group": should_be_group,
						}
					)

					try:
						cost_center_doc.insert(ignore_permissions=True)
					except frappe.DuplicateEntryError:
						pass
					except Exception:
						self.log(cost_center_doc)

				if len(still_pending) == len(pending):
					for row in still_pending:
						cost_center_name = row.get("cost_center_name") or row.get("name")
						if not cost_center_name:
							continue

						should_be_group = 1 if row.get("is_group") or cost_center_name in parent_names else 0

						existing = get_cost_center_name(cost_center_name)
						if existing:
							if should_be_group:
								frappe.db.set_value("Cost Center", existing, "is_group", 1)
							continue

						cost_center_doc = frappe.get_doc(
							{
								"doctype": "Cost Center",
								"cost_center_name": cost_center_name,
								"company": self.erpnext_company,
								"parent_cost_center": root_cost_center,
								"is_group": should_be_group,
							}
						)

						try:
							cost_center_doc.insert(ignore_permissions=True)
						except frappe.DuplicateEntryError:
							pass
						except Exception:
							self.log(cost_center_doc)

					break

				pending = still_pending

			frappe.db.commit()


		def create_party_groups(customer_groups_file_url, supplier_groups_file_url):
		        def create_customer_groups(customer_groups):
		                customer_group_names = {
		                        row.get("customer_group_name")
		                        for row in customer_groups
		                        if row.get("customer_group_name") and row.get("customer_group_name") != "All Customer Groups"
		                }

		                parent_group_names = {
		                        row.get("parent_customer_group")
		                        for row in customer_groups
		                        if row.get("parent_customer_group")
		                        and row.get("parent_customer_group") != "All Customer Groups"
		                        and row.get("parent_customer_group") != row.get("customer_group_name")
		                }

		                pending = [
		                        row
		                        for row in customer_groups
		                        if row.get("customer_group_name")
		                        and row.get("customer_group_name") != "All Customer Groups"
		                ]

		                max_rounds = len(pending) + 5

		                for _round in range(max_rounds):
		                        if not pending:
		                                break

		                        still_pending = []

		                        for row in pending:
		                                group_name = row.get("customer_group_name")
		                                parent_group = row.get("parent_customer_group") or "All Customer Groups"
		                                is_group = 1 if group_name in parent_group_names else 0

		                                if parent_group == group_name:
		                                        parent_group = "All Customer Groups"

		                                if not frappe.db.exists("Customer Group", parent_group):
		                                        if parent_group in customer_group_names:
		                                                still_pending.append(row)
		                                                continue

		                                        parent_group = "All Customer Groups"

		                                try:
		                                        if frappe.db.exists("Customer Group", group_name):
		                                                frappe.db.set_value("Customer Group", group_name, "is_group", is_group)
		                                                continue

		                                        doc = frappe.get_doc(
		                                                {
		                                                        "doctype": "Customer Group",
		                                                        "customer_group_name": group_name,
		                                                        "parent_customer_group": parent_group,
		                                                        "is_group": is_group,
		                                                }
		                                        )
		                                        doc.insert(ignore_permissions=True)
		                                except frappe.DuplicateEntryError:
		                                        frappe.clear_messages()
		                                except Exception:
		                                        self.log(row)

		                        if len(still_pending) == len(pending):
		                                for row in still_pending:
		                                        group_name = row.get("customer_group_name")
		                                        if not group_name:
		                                                continue

		                                        try:
		                                                if frappe.db.exists("Customer Group", group_name):
		                                                        frappe.db.set_value("Customer Group", group_name, "is_group", 0)
		                                                        continue

		                                                doc = frappe.get_doc(
		                                                        {
		                                                                "doctype": "Customer Group",
		                                                                "customer_group_name": group_name,
		                                                                "parent_customer_group": "All Customer Groups",
		                                                                "is_group": 0,
		                                                        }
		                                                )
		                                                doc.insert(ignore_permissions=True)
		                                        except Exception:
		                                                self.log(row)

		                                break

		                        pending = still_pending

		        def create_supplier_groups(supplier_groups):
		                supplier_group_names = {
		                        row.get("supplier_group_name")
		                        for row in supplier_groups
		                        if row.get("supplier_group_name") and row.get("supplier_group_name") != "All Supplier Groups"
		                }

		                parent_group_names = {
		                        row.get("parent_supplier_group")
		                        for row in supplier_groups
		                        if row.get("parent_supplier_group")
		                        and row.get("parent_supplier_group") != "All Supplier Groups"
		                        and row.get("parent_supplier_group") != row.get("supplier_group_name")
		                }

		                pending = [
		                        row
		                        for row in supplier_groups
		                        if row.get("supplier_group_name")
		                        and row.get("supplier_group_name") != "All Supplier Groups"
		                ]

		                max_rounds = len(pending) + 5

		                for _round in range(max_rounds):
		                        if not pending:
		                                break

		                        still_pending = []

		                        for row in pending:
		                                group_name = row.get("supplier_group_name")
		                                parent_group = row.get("parent_supplier_group") or "All Supplier Groups"
		                                is_group = 1 if group_name in parent_group_names else 0

		                                if parent_group == group_name:
		                                        parent_group = "All Supplier Groups"

		                                if not frappe.db.exists("Supplier Group", parent_group):
		                                        if parent_group in supplier_group_names:
		                                                still_pending.append(row)
		                                                continue

		                                        parent_group = "All Supplier Groups"

		                                try:
		                                        if frappe.db.exists("Supplier Group", group_name):
		                                                frappe.db.set_value("Supplier Group", group_name, "is_group", is_group)
		                                                continue

		                                        doc = frappe.get_doc(
		                                                {
		                                                        "doctype": "Supplier Group",
		                                                        "supplier_group_name": group_name,
		                                                        "parent_supplier_group": parent_group,
		                                                        "is_group": is_group,
		                                                }
		                                        )
		                                        doc.insert(ignore_permissions=True)
		                                except frappe.DuplicateEntryError:
		                                        frappe.clear_messages()
		                                except Exception:
		                                        self.log(row)

		                        if len(still_pending) == len(pending):
		                                for row in still_pending:
		                                        group_name = row.get("supplier_group_name")
		                                        if not group_name:
		                                                continue

		                                        try:
		                                                if frappe.db.exists("Supplier Group", group_name):
		                                                        frappe.db.set_value("Supplier Group", group_name, "is_group", 0)
		                                                        continue

		                                                doc = frappe.get_doc(
		                                                        {
		                                                                "doctype": "Supplier Group",
		                                                                "supplier_group_name": group_name,
		                                                                "parent_supplier_group": "All Supplier Groups",
		                                                                "is_group": 0,
		                                                        }
		                                                )
		                                                doc.insert(ignore_permissions=True)
		                                        except Exception:
		                                                self.log(row)

		                                break

		                        pending = still_pending

		        customer_groups = load_processed_json(customer_groups_file_url)
		        supplier_groups = load_processed_json(supplier_groups_file_url)

		        create_customer_groups(customer_groups)
		        create_supplier_groups(supplier_groups)
		def create_parties_and_addresses(parties_file_url, addresses_file_url):
		        def ensure_leaf_customer_group(group_name):
		                group_name = group_name or "Tally Customers"

		                if frappe.db.exists("Customer Group", group_name):
		                        if not frappe.db.get_value("Customer Group", group_name, "is_group"):
		                                return group_name

		                        leaf_group = f"{group_name} Customers"
		                        if not frappe.db.exists("Customer Group", leaf_group):
		                                doc = frappe.get_doc(
		                                        {
		                                                "doctype": "Customer Group",
		                                                "customer_group_name": leaf_group,
		                                                "parent_customer_group": group_name,
		                                                "is_group": 0,
		                                        }
		                                )
		                                doc.insert(ignore_permissions=True)

		                        return leaf_group

		                doc = frappe.get_doc(
		                        {
		                                "doctype": "Customer Group",
		                                "customer_group_name": group_name,
		                                "parent_customer_group": "All Customer Groups",
		                                "is_group": 0,
		                        }
		                )
		                doc.insert(ignore_permissions=True)
		                return doc.name

		        def ensure_leaf_supplier_group(group_name):
		                group_name = group_name or "Tally Suppliers"

		                if frappe.db.exists("Supplier Group", group_name):
		                        if not frappe.db.get_value("Supplier Group", group_name, "is_group"):
		                                return group_name

		                        leaf_group = f"{group_name} Suppliers"
		                        if not frappe.db.exists("Supplier Group", leaf_group):
		                                doc = frappe.get_doc(
		                                        {
		                                                "doctype": "Supplier Group",
		                                                "supplier_group_name": leaf_group,
		                                                "parent_supplier_group": group_name,
		                                                "is_group": 0,
		                                        }
		                                )
		                                doc.insert(ignore_permissions=True)

		                        return leaf_group

		                doc = frappe.get_doc(
		                        {
		                                "doctype": "Supplier Group",
		                                "supplier_group_name": group_name,
		                                "parent_supplier_group": "All Supplier Groups",
		                                "is_group": 0,
		                        }
		                )
		                doc.insert(ignore_permissions=True)
		                return doc.name

		        parties = load_processed_json(parties_file_url)

		        for party in parties:
		                try:
		                        if party.get("doctype") == "Customer":
		                                if frappe.db.exists({"doctype": "Customer", "customer_name": party.get("customer_name")}):
		                                        continue

		                                party["customer_group"] = ensure_leaf_customer_group(party.get("customer_group"))

		                        if party.get("doctype") == "Supplier":
		                                if frappe.db.exists({"doctype": "Supplier", "supplier_name": party.get("supplier_name")}):
		                                        continue

		                                party["supplier_group"] = ensure_leaf_supplier_group(party.get("supplier_group"))

		                        party_doc = frappe.get_doc(party)
		                        party_doc.insert(ignore_permissions=True)
		                except frappe.DuplicateEntryError:
		                        frappe.clear_messages()
		                except Exception:
		                        self.log(party)

		        addresses = load_processed_json(addresses_file_url)

		        for address in addresses:
		                try:
		                        valid_links = []
		                        for link in address.get("links") or []:
		                                link_doctype = link.get("link_doctype")
		                                link_name = link.get("link_name")

		                                if link_doctype and link_name and frappe.db.exists(link_doctype, link_name):
		                                        valid_links.append(link)

		                        if not valid_links:
		                                continue

		                        address["links"] = valid_links
		                        address_doc = frappe.get_doc(address)
		                        address_doc.insert(ignore_permissions=True, ignore_mandatory=True)
		                except frappe.DuplicateEntryError:
		                        frappe.clear_messages()
		                except Exception:
		                        self.log(address)
		def create_uoms(uoms_file_url):
			uoms = load_processed_json(uoms_file_url)

			for uom in uoms:
				uom_name = uom.get("uom_name")
				if not uom_name:
					continue

				if frappe.db.exists("UOM", uom_name):
					continue

				try:
					uom_doc = frappe.get_doc(uom)
					uom_doc.insert(ignore_permissions=True)
				except frappe.DuplicateEntryError:
					pass
				except Exception:
					self.log(uom)

		def create_items(items_file_url, stock_items_enhanced_file_url=None):
			enhanced_items = load_processed_json(stock_items_enhanced_file_url)
			original_items = load_processed_json(items_file_url)

			if enhanced_items:
				items = enhanced_items
			else:
				items = original_items

			for item in items:
				item_code = item.get("item_code")
				if not item_code:
					continue

				if frappe.db.exists("Item", item_code):
					continue

				stock_uom = item.get("stock_uom") or self.default_uom

				if stock_uom and not frappe.db.exists("UOM", stock_uom):
					try:
						frappe.get_doc({"doctype": "UOM", "uom_name": stock_uom}).insert(ignore_permissions=True)
					except Exception:
						pass

				item_group = item.get("stock_group") or item.get("item_group") or "All Item Groups"

				if item_group in ("Primary", None, ""):
					item_group = "All Item Groups"

				if not frappe.db.exists("Item Group", item_group):
					item_group = "All Item Groups"

				item_doc = frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": item_code,
						"item_name": item.get("item_name") or item_code,
						"stock_uom": stock_uom,
						"is_stock_item": 1,
						"item_group": item_group,
						"item_defaults": [{"company": self.erpnext_company}],
					}
				)

				try:
					item_doc.insert(ignore_permissions=True)
				except frappe.DuplicateEntryError:
					pass
				except Exception:
					self.log(item_doc)

		def normalize_tally_date(value):
			value = str(value or "").strip()

			if len(value) == 8 and value.isdigit():
				return value[:4] + "-" + value[4:6] + "-" + value[6:8]

			return value or None

		def create_departments_designations_employees(employee_groups_file_url, employees_file_url):
			def ensure_department(department_name):
				if not department_name:
					return None

				existing = frappe.db.get_value("Department", {"department_name": department_name}, "name")
				if existing:
					return existing

				department = frappe.get_doc(
					{
						"doctype": "Department",
						"department_name": department_name,
						"company": self.erpnext_company,
					}
				)

				try:
					department.insert(ignore_permissions=True)
					return department.name
				except frappe.DuplicateEntryError:
					return frappe.db.get_value("Department", {"department_name": department_name}, "name")
				except Exception:
					self.log(department)
					return None

			def ensure_designation(designation_name):
				if not designation_name:
					return None

				if frappe.db.exists("Designation", designation_name):
					return designation_name

				designation = frappe.get_doc(
					{
						"doctype": "Designation",
						"designation_name": designation_name,
					}
				)

				try:
					designation.insert(ignore_permissions=True)
					return designation.name
				except frappe.DuplicateEntryError:
					return designation_name
				except Exception:
					self.log(designation)
					return None

			employees = load_processed_json(employees_file_url)
			employee_groups = load_processed_json(employee_groups_file_url)

			for row in employee_groups:
				if row.get("tally_master_type") == "EMPLOYEEGROUP":
					ensure_department(row.get("name"))

			for row in employees:
				employee_name = row.get("employee_name")
				if not employee_name:
					continue

				department = ensure_department(row.get("department") or row.get("parent"))
				designation = ensure_designation(row.get("designation"))

				existing = frappe.db.get_value(
					"Employee",
					{
						"employee_name": employee_name,
						"company": self.erpnext_company,
					},
					"name",
				)

				if existing:
					continue

				employee_doc = frappe.get_doc(
					{
						"doctype": "Employee",
						"employee_name": employee_name,
						"first_name": employee_name,
						"employee_number": row.get("employee_number"),
						"company": self.erpnext_company,
						"department": department,
						"designation": designation,
						"date_of_joining": normalize_tally_date(row.get("date_of_joining")),
						"date_of_birth": normalize_tally_date(row.get("date_of_birth")),
						"gender": row.get("gender"),
						"pan_number": row.get("pan_number"),
						"status": "Active",
					}
				)

				try:
					employee_doc.insert(ignore_permissions=True)
				except frappe.DuplicateEntryError:
					pass
				except Exception:
					self.log(employee_doc)

		def create_salary_components_if_available(payheads_file_url):
			if not frappe.db.exists("DocType", "Salary Component"):
				return

			payheads = load_processed_json(payheads_file_url)

			for row in payheads:
				salary_component = row.get("salary_component") or row.get("tally_name")
				if not salary_component:
					continue

				if frappe.db.exists("Salary Component", salary_component):
					continue

				component_type = "Earning"
				payhead_type = str(row.get("payhead_type") or "").lower()
				income_type = str(row.get("income_type") or "").lower()

				if "deduction" in payhead_type or "deduction" in income_type:
					component_type = "Deduction"

				component = frappe.get_doc(
					{
						"doctype": "Salary Component",
						"salary_component": salary_component,
						"salary_component_abbr": row.get("salary_component_abbr") or salary_component[:20],
						"type": component_type,
						"company": self.erpnext_company,
					}
				)

				try:
					component.insert(ignore_permissions=True)
				except frappe.DuplicateEntryError:
					pass
				except Exception:
					self.log(component)

		try:
			default_currency = get_default_currency()

			self.publish("Import Master Data", _("Importing Currencies"), 1, 8)
			create_currencies(self.currencies)

			self.publish("Import Master Data", _("Creating Company and Importing Chart of Accounts"), 2, 8)
			create_company_and_coa(self.chart_of_accounts, default_currency)
			self._ensure_round_off_account(self.erpnext_company)

			self.publish("Import Master Data", _("Importing Item Groups, Warehouses and Cost Centers"), 3, 8)
			create_item_groups(self.item_groups)
			create_warehouses(self.warehouses)
			create_cost_centers(self.cost_centers)

			self.publish("Import Master Data", _("Importing Party Groups, Parties and Addresses"), 4, 8)
			create_party_groups(getattr(self, "customer_groups", None), getattr(self, "supplier_groups", None))
			create_parties_and_addresses(self.parties, self.addresses)

			self.publish("Import Master Data", _("Importing UOMs"), 5, 8)
			create_uoms(self.uoms)

			self.publish("Import Master Data", _("Importing Items"), 6, 8)
			create_items(self.items, self.stock_items_enhanced)

			self.publish("Import Master Data", _("Importing Employees and Payroll Masters"), 7, 8)
			create_departments_designations_employees(self.employee_groups, self.employees)
			create_salary_components_if_available(getattr(self, "payheads", None))

			self.publish("Import Master Data", _("Done"), 8, 8)

			self.set_account_defaults()
			self.is_master_data_imported = 1
			frappe.db.commit()

		except Exception:
			self.publish("Import Master Data", _("Process Failed"), -1, 8)
			frappe.db.rollback()
			self.log()

		finally:
			self.set_status()


	@frappe.whitelist()
	def _import_opening_balances(self):
		import json

		self.reload()

		if not self.is_master_data_imported:
			frappe.throw(_("Please import Master Data before importing Opening Balances."))

		if getattr(self, "is_opening_balances_imported", 0):
			return {"status": "skipped", "message": "Opening Balances already imported"}

		if not getattr(self, "opening_posting_date", None):
			frappe.throw(_("Please set Opening Posting Date before importing Opening Balances."))

		posting_date = self.opening_posting_date

		def load_processed_json(file_url):
			if not file_url:
				return []

			file_doc = frappe.get_doc("File", {"file_url": file_url})
			return json.loads(file_doc.get_content())

		def get_company_currency():
			return frappe.db.get_value("Company", self.erpnext_company, "default_currency")

		def get_default_uom():
			return self.default_uom or frappe.db.get_single_value("Stock Settings", "stock_uom") or "Unit"

		def get_default_cost_center():
			return self.default_cost_center or frappe.db.get_value("Company", self.erpnext_company, "cost_center")

		def get_temporary_opening_account():
			account = frappe.db.get_value(
				"Account",
				{
					"company": self.erpnext_company,
					"account_type": "Temporary",
					"is_group": 0,
				},
				"name",
			)

			if not account:
				frappe.throw(_("Please add a Temporary Opening account in Chart of Accounts for company {0}.").format(self.erpnext_company))

			return account

		def get_stock_adjustment_account():
			return (
				frappe.db.get_value("Company", self.erpnext_company, "stock_adjustment_account")
				or get_temporary_opening_account()
			)

		def clean_name_part(value, max_length=50):
			value = str(value or "").strip()

			for char in ["/", "\\", ":", "*", "?", '"', "<", ">", "|", "\n", "\r", "\t"]:
				value = value.replace(char, "-")

			value = " ".join(value.split())

			if not value:
				value = "Opening"

			return value[:max_length].strip(" .-") or "Opening"

		def make_invoice_name(prefix, invoice_number, party):
			invoice_part = clean_name_part(invoice_number, 45)
			party_part = clean_name_part(party, 55)
			return ("TALLY-" + prefix + "-" + invoice_part + "-" + party_part)[:140]

		def create_opening_invoice(row, invoice_type):
			if row.get("is_advance"):
				return "skipped_advance"

			outstanding_amount = abs(float(row.get("outstanding_amount") or 0))

			if not outstanding_amount:
				return "skipped_zero"

			party_field = "customer" if invoice_type == "Sales" else "supplier"
			party_type = "Customer" if invoice_type == "Sales" else "Supplier"
			doctype = "Sales Invoice" if invoice_type == "Sales" else "Purchase Invoice"
			account_field = "income_account" if invoice_type == "Sales" else "expense_account"
			prefix = "SI" if invoice_type == "Sales" else "PI"

			party = row.get(party_field) or row.get("party")

			if not party or not frappe.db.exists(party_type, party):
				return "skipped_missing_party"

			invoice_name = make_invoice_name(prefix, row.get("invoice_number"), party)

			if frappe.db.exists(doctype, invoice_name):
				return "skipped_existing"

			invoice_posting_date = row.get("posting_date") or posting_date
			due_date = row.get("due_date") or invoice_posting_date

			item = {
				"item_name": "Opening Invoice Item",
				"description": "Opening balance imported from Tally bill " + str(row.get("invoice_number") or ""),
				"uom": get_default_uom(),
				"qty": 1,
				"conversion_factor": 1,
				"rate": outstanding_amount,
				account_field: temporary_opening_account,
				"cost_center": default_cost_center,
			}

			invoice = {
				"doctype": doctype,
				"company": self.erpnext_company,
				"currency": company_currency,
				"is_opening": "Yes",
				"set_posting_time": 1,
				"posting_date": invoice_posting_date,
				"due_date": due_date,
				"update_stock": 0,
				"is_pos": 0,
				"disable_rounded_total": 1,
				party_field: party,
				"items": [item],
				"remarks": "Opening balance imported from Tally. Tally Bill No: " + str(row.get("invoice_number") or ""),
			}

			if invoice_type == "Purchase":
				invoice["bill_no"] = str(row.get("invoice_number") or invoice_name)[:140]
				invoice["bill_date"] = invoice_posting_date

			invoice_doc = frappe.get_doc(invoice)
			invoice_doc.flags.ignore_mandatory = True
			invoice_doc.insert(ignore_permissions=True, set_name=invoice_name)
			invoice_doc.submit()

			return "created"

		def get_warehouse(warehouse_name):
			if warehouse_name:
				warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"warehouse_name": warehouse_name,
						"company": self.erpnext_company,
					},
					"name",
				)

				if warehouse:
					return warehouse

				if frappe.db.exists("Warehouse", warehouse_name):
					return warehouse_name

			if self.default_warehouse:
				return self.default_warehouse

			return frappe.db.get_value(
				"Warehouse",
				{
					"company": self.erpnext_company,
					"is_group": 0,
				},
				"name",
			)

		def create_stock_reconciliation(opening_stock_rows):
			aggregated = {}

			for row in opening_stock_rows:
				item_code = row.get("item_code")
				warehouse = get_warehouse(row.get("warehouse_name"))

				if not item_code or not warehouse:
					continue

				qty = abs(float(row.get("qty") or 0))
				opening_value = abs(float(row.get("opening_value") or 0))
				valuation_rate = abs(float(row.get("valuation_rate") or 0))

				if not qty:
					continue

				if not opening_value and valuation_rate:
					opening_value = qty * valuation_rate

				key = (item_code, warehouse)

				if key not in aggregated:
					aggregated[key] = {
						"item_code": item_code,
						"warehouse": warehouse,
						"qty": 0,
						"opening_value": 0,
					}

				aggregated[key]["qty"] += qty
				aggregated[key]["opening_value"] += opening_value

			items = []

			for row in aggregated.values():
				qty = row["qty"]
				opening_value = row["opening_value"]
				valuation_rate = opening_value / qty if qty else 0

				items.append(
					{
						"item_code": row["item_code"],
						"warehouse": row["warehouse"],
						"qty": qty,
						"valuation_rate": valuation_rate,
					}
				)

			if not items:
				return None, 0

			stock_reconciliation = frappe.get_doc(
				{
					"doctype": "Stock Reconciliation",
					"company": self.erpnext_company,
					"purpose": "Opening Stock",
					"posting_date": posting_date,
					"set_posting_time": 1,
					"expense_account": get_stock_adjustment_account(),
					"items": items,
					"remarks": "Opening stock imported from Tally Migration " + self.name,
				}
			)

			stock_reconciliation.flags.ignore_mandatory = True
			stock_reconciliation.insert(ignore_permissions=True)
			stock_reconciliation.submit()

			return stock_reconciliation.name, len(items)

		sales_rows = load_processed_json(getattr(self, "opening_sales_invoices", None))
		purchase_rows = load_processed_json(getattr(self, "opening_purchase_invoices", None))
		stock_rows = load_processed_json(getattr(self, "opening_stock_items", None))

		company_currency = get_company_currency()
		default_cost_center = get_default_cost_center()
		temporary_opening_account = get_temporary_opening_account()

		if not default_cost_center:
			frappe.throw(_("Please set Default Cost Center before importing Opening Balances."))

		summary = {
			"sales_created": 0,
			"purchase_created": 0,
			"stock_reconciliation": None,
			"stock_rows": 0,
			"skipped_existing": 0,
			"skipped_zero": 0,
			"skipped_advance": 0,
			"skipped_missing_party": 0,
		}

		try:
			self.publish("Import Opening Balances", _("Creating Opening Sales Invoices"), 1, 4)

			for row in sales_rows:
				result = create_opening_invoice(row, "Sales")

				if result == "created":
					summary["sales_created"] += 1
				elif result in summary:
					summary[result] += 1

			frappe.db.commit()

			self.publish("Import Opening Balances", _("Creating Opening Purchase Invoices"), 2, 4)

			for row in purchase_rows:
				result = create_opening_invoice(row, "Purchase")

				if result == "created":
					summary["purchase_created"] += 1
				elif result in summary:
					summary[result] += 1

			frappe.db.commit()

			self.publish("Import Opening Balances", _("Creating Opening Stock Reconciliation"), 3, 4)

			stock_reconciliation_name, stock_row_count = create_stock_reconciliation(stock_rows)
			summary["stock_reconciliation"] = stock_reconciliation_name
			summary["stock_rows"] = stock_row_count

			frappe.db.commit()

			self.publish("Import Opening Balances", _("Finalizing Opening Balance Import"), 4, 4)

			self.is_opening_balances_imported = 1
			self.save(ignore_permissions=True)
			frappe.db.commit()

			return summary

		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Tally Opening Balance Import Failed")
			raise


	@frappe.whitelist()
	def _import_opening_journal_entry(self):
		import json

		self.reload()

		if not self.is_master_data_imported:
			frappe.throw(_("Please import Master Data before importing Opening Journal Entry."))

		if getattr(self, "is_opening_journal_entry_imported", 0):
			return {
				"status": "skipped",
				"message": "Opening Journal Entry already imported",
				"journal_entry": getattr(self, "opening_journal_entry", None),
			}

		if not getattr(self, "opening_posting_date", None):
			frappe.throw(_("Please set Opening Posting Date before importing Opening Journal Entry."))

		def load_processed_json(file_url):
			if not file_url:
				return []

			file_doc = frappe.get_doc("File", {"file_url": file_url})
			return json.loads(file_doc.get_content())

		def clean_number_value(value):
			try:
				return float(str(value or "0").replace(",", "").strip())
			except Exception:
				return 0.0

		def get_temporary_opening_account():
			account = frappe.db.get_value(
				"Account",
				{
					"company": self.erpnext_company,
					"account_type": "Temporary",
					"is_group": 0,
				},
				"name",
			)

			if not account:
				frappe.throw(_("Please add a Temporary Opening account in Chart of Accounts for company {0}.").format(self.erpnext_company))

			return account

		def get_party_names():
			parties = load_processed_json(getattr(self, "parties", None))
			party_names = set()

			for party in parties:
				if party.get("doctype") == "Customer" and party.get("customer_name"):
					party_names.add(party.get("customer_name"))

				if party.get("doctype") == "Supplier" and party.get("supplier_name"):
					party_names.add(party.get("supplier_name"))

			return party_names

		def get_account_for_ledger(ledger_name, temporary_opening_account):
			if ledger_name == "PROFIT & LOSS A/C":
				return temporary_opening_account, "Mapped reserved Tally ledger PROFIT & LOSS A/C to Temporary Opening"

			account = frappe.db.get_value(
				"Account",
				{
					"account_name": ledger_name,
					"company": self.erpnext_company,
					"is_group": 0,
				},
				"name",
			)

			if account:
				return account, None

			account = frappe.db.get_value(
				"Account",
				{
					"name": ledger_name,
					"company": self.erpnext_company,
					"is_group": 0,
				},
				"name",
			)

			if account:
				return account, None

			return None, "Missing leaf account for ledger " + str(ledger_name)

		def add_journal_row(rows, account, debit, credit, user_remark):
			if not account:
				return

			debit = round(float(debit or 0), 2)
			credit = round(float(credit or 0), 2)

			if not debit and not credit:
				return

			rows.append(
				{
					"account": account,
					"debit_in_account_currency": debit,
					"credit_in_account_currency": credit,
					"user_remark": user_remark,
				}
			)

		ledger_opening_rows = load_processed_json(getattr(self, "ledger_opening_balances", None))
		party_names = get_party_names()
		temporary_opening_account = get_temporary_opening_account()

		seen = set()
		journal_rows = []
		skipped_party_ledgers = 0
		skipped_duplicates = 0
		missing_accounts = []
		mapped_reserved_ledgers = []

		for row in ledger_opening_rows:
			ledger_name = row.get("ledger_name") or row.get("account") or row.get("name") or row.get("account_name")
			parent = row.get("parent")
			opening_balance_text = str(row.get("opening_balance") or "0").strip()

			if not ledger_name:
				continue

			if ledger_name in party_names:
				skipped_party_ledgers += 1
				continue

			dedupe_key = (ledger_name, parent, opening_balance_text)

			if dedupe_key in seen:
				skipped_duplicates += 1
				continue

			seen.add(dedupe_key)

			opening_balance = clean_number_value(opening_balance_text)

			if not opening_balance:
				continue

			account, mapping_note = get_account_for_ledger(ledger_name, temporary_opening_account)

			if not account:
				missing_accounts.append(
					{
						"ledger_name": ledger_name,
						"parent": parent,
						"opening_balance": opening_balance_text,
						"reason": mapping_note,
					}
				)
				continue

			if mapping_note:
				mapped_reserved_ledgers.append(
					{
						"ledger_name": ledger_name,
						"account": account,
						"opening_balance": opening_balance_text,
						"note": mapping_note,
					}
				)

			debit = abs(opening_balance) if opening_balance < 0 else 0
			credit = abs(opening_balance) if opening_balance > 0 else 0

			add_journal_row(
				journal_rows,
				account,
				debit,
				credit,
				"Opening balance imported from Tally ledger " + str(ledger_name),
			)

		if missing_accounts:
			frappe.throw(
				_("Cannot create Opening Journal Entry because some non-party ledgers could not be mapped to ERPNext Accounts: {0}").format(
					frappe.as_json(missing_accounts[:20])
				)
			)

		if not journal_rows:
			frappe.throw(_("No non-party opening ledger balances found to import."))

		total_debit = round(sum(row.get("debit_in_account_currency") or 0 for row in journal_rows), 2)
		total_credit = round(sum(row.get("credit_in_account_currency") or 0 for row in journal_rows), 2)
		difference = round(total_debit - total_credit, 2)

		if difference > 0:
			add_journal_row(
				journal_rows,
				temporary_opening_account,
				0,
				abs(difference),
				"Balancing line for Tally opening ledger balances",
			)
		elif difference < 0:
			add_journal_row(
				journal_rows,
				temporary_opening_account,
				abs(difference),
				0,
				"Balancing line for Tally opening ledger balances",
			)

		final_total_debit = round(sum(row.get("debit_in_account_currency") or 0 for row in journal_rows), 2)
		final_total_credit = round(sum(row.get("credit_in_account_currency") or 0 for row in journal_rows), 2)

		if final_total_debit != final_total_credit:
			frappe.throw(
				_("Opening Journal Entry is not balanced. Debit {0}, Credit {1}.").format(
					final_total_debit,
					final_total_credit,
				)
			)

		try:
			self.publish("Import Opening Journal Entry", _("Creating Opening Journal Entry"), 1, 2)

			journal_entry = frappe.get_doc(
				{
					"doctype": "Journal Entry",
					"voucher_type": "Opening Entry",
					"company": self.erpnext_company,
					"posting_date": self.opening_posting_date,
					"user_remark": "Opening Journal Entry imported from Tally Migration " + self.name,
					"accounts": journal_rows,
					"multi_currency": 0,
				}
			)

			journal_entry.flags.ignore_mandatory = True
			journal_entry.insert(ignore_permissions=True)
			journal_entry.submit()

			self.publish("Import Opening Journal Entry", _("Finalizing Opening Journal Entry"), 2, 2)

			self.opening_journal_entry = journal_entry.name
			self.is_opening_journal_entry_imported = 1
			self.save(ignore_permissions=True)

			frappe.db.commit()

			return {
				"status": "created",
				"journal_entry": journal_entry.name,
				"rows": len(journal_rows),
				"total_debit": final_total_debit,
				"total_credit": final_total_credit,
				"skipped_party_ledgers": skipped_party_ledgers,
				"skipped_duplicates": skipped_duplicates,
				"mapped_reserved_ledgers": mapped_reserved_ledgers,
			}

		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Tally Opening Journal Entry Import Failed")
			raise

	def get_default_erpnext_doctype_for_tally_voucher(self, tally_voucher_type, has_inventory_entries=False):
		voucher_type = str(tally_voucher_type or "").strip().lower()

		default_map = {
			"journal": "Journal Entry",
			"receipt": "Journal Entry",
			"payment": "Journal Entry",
			"contra": "Journal Entry",
			"sales": "Sales Invoice",
			"einvoice": "Sales Invoice",
			"e-invoice": "Sales Invoice",
			"purchase": "Purchase Invoice",
			"credit note": "Sales Invoice",
			"debit note": "Purchase Invoice",
			"delivery note": "Delivery Note",
			"receipt note": "Purchase Receipt",
			"sales order": "Sales Order",
			"purchase order": "Purchase Order",
			"quotation": "Quotation",
			"quote": "Quotation",
			"sales quote": "Quotation",
			"stock journal": "Stock Entry",
		}

		if voucher_type in default_map:
			return default_map[voucher_type]

		if has_inventory_entries:
			return "Sales Invoice"

		return "Journal Entry"

	def get_voucher_type_mapping_key(self, tally_voucher_type, tally_persisted_view=None):
		return (
			str(tally_voucher_type or "").strip(),
			str(tally_persisted_view or "").strip(),
		)

	def build_voucher_type_mappings_from_daybook(self, collection):
		mappings = []
		seen = set()

		def get_child_text(tag, child_name):
			child = tag.find(child_name)
			return child.get_text(strip=True) if child else None

		for voucher in collection.find_all("VOUCHER"):
			is_cancelled = get_child_text(voucher, "ISCANCELLED")
			if str(is_cancelled or "").strip().lower() == "yes":
				continue

			tally_voucher_type = (
				voucher.get("VCHTYPE")
				or get_child_text(voucher, "VOUCHERTYPENAME")
				or ""
			).strip()

			if not tally_voucher_type:
				continue

			tally_persisted_view = (
				voucher.get("OBJVIEW")
				or get_child_text(voucher, "PERSISTEDVIEW")
				or ""
			).strip()

			key = self.get_voucher_type_mapping_key(tally_voucher_type, tally_persisted_view)
			if key in seen:
				continue

			seen.add(key)

			inventory_entries = (
				voucher.find_all("INVENTORYENTRIES.LIST")
				+ voucher.find_all("ALLINVENTORYENTRIES.LIST")
				+ voucher.find_all("INVENTORYENTRIESIN.LIST")
				+ voucher.find_all("INVENTORYENTRIESOUT.LIST")
			)

			erpnext_doctype = self.get_default_erpnext_doctype_for_tally_voucher(
				tally_voucher_type,
				has_inventory_entries=bool(inventory_entries),
			)

			lower_type = tally_voucher_type.lower()

			mappings.append(
				{
					"doctype": "Tally Voucher Type Mapping",
					"tally_voucher_type": tally_voucher_type,
					"tally_persisted_view": tally_persisted_view,
					"erpnext_doctype": erpnext_doctype,
					"erpnext_voucher_type": "Material Transfer" if erpnext_doctype == "Stock Entry" else "",
					"import_with_inventory": 1 if inventory_entries else 0,
					"is_return": 1 if lower_type in ("credit note", "debit note") else 0,
					"enabled": 1,
				}
			)

		mappings.sort(
			key=lambda row: (
				row.get("tally_voucher_type") or "",
				row.get("tally_persisted_view") or "",
			)
		)

		return mappings

	def upsert_voucher_type_mappings_from_daybook(self, collection):
		existing_rows = {}
		for row in self.get("voucher_type_mappings") or []:
			key = self.get_voucher_type_mapping_key(
				row.get("tally_voucher_type"),
				row.get("tally_persisted_view"),
			)
			existing_rows[key] = row

		discovered_rows = self.build_voucher_type_mappings_from_daybook(collection)

		for discovered in discovered_rows:
			key = self.get_voucher_type_mapping_key(
				discovered.get("tally_voucher_type"),
				discovered.get("tally_persisted_view"),
			)

			if key in existing_rows:
				continue

			self.append("voucher_type_mappings", discovered)



	def _process_day_book_data(self, raise_on_failure=False):
		def get_inventory_entries(voucher):
			return (
				voucher.find_all("INVENTORYENTRIES.LIST")
				+ voucher.find_all("ALLINVENTORYENTRIES.LIST")
				+ voucher.find_all("INVENTORYENTRIESIN.LIST")
				+ voucher.find_all("INVENTORYENTRIESOUT.LIST")
			)

		def get_voucher_type_mapping(voucher):
			tally_voucher_type = (
				voucher.get("VCHTYPE")
				or get_child_text(voucher, "VOUCHERTYPENAME")
				or ""
			).strip()

			tally_persisted_view = (
				voucher.get("OBJVIEW")
				or get_child_text(voucher, "PERSISTEDVIEW")
				or ""
			).strip()

			mapping_by_key = {}
			for row in self.get("voucher_type_mappings") or []:
				key = self.get_voucher_type_mapping_key(
					row.get("tally_voucher_type"),
					row.get("tally_persisted_view"),
				)
				mapping_by_key[key] = row

			return mapping_by_key.get(
				self.get_voucher_type_mapping_key(tally_voucher_type, tally_persisted_view)
			) or mapping_by_key.get(
				self.get_voucher_type_mapping_key(tally_voucher_type, "")
			)

		def get_tally_cost_center_from_allocations(tag):
			for category_allocation in tag.find_all("CATEGORYALLOCATIONS.LIST"):
				for cost_center_allocation in category_allocation.find_all("COSTCENTREALLOCATIONS.LIST"):
					cost_center_name = get_child_text(cost_center_allocation, "NAME")
					if cost_center_name:
						return encode_company_abbr(cost_center_name, self.erpnext_company)

			return self.default_cost_center

		def get_voucher_converter(voucher):
			mapping = get_voucher_type_mapping(voucher)
			inventory_entries = get_inventory_entries(voucher)

			if mapping and not mapping.get("enabled"):
				return None, None

			erpnext_doctype = mapping.get("erpnext_doctype") if mapping else None

			if not erpnext_doctype:
				voucher_type = voucher.VOUCHERTYPENAME.string.strip()
				if voucher_type not in ["Journal", "Receipt", "Payment", "Contra"] and inventory_entries:
					erpnext_doctype = "Sales Invoice"
				else:
					erpnext_doctype = "Journal Entry"

			if erpnext_doctype == "Journal Entry":
				return voucher_to_journal_entry, erpnext_doctype

			if erpnext_doctype in ["Sales Invoice", "Purchase Invoice"]:
				return voucher_to_invoice, erpnext_doctype

			if erpnext_doctype in [
				"Delivery Note",
				"Purchase Receipt",
				"Sales Order",
				"Purchase Order",
				"Quotation",
				"Stock Entry",
			]:
				return voucher_to_inventory_document, erpnext_doctype

			return None, erpnext_doctype

		processing_failures = []

		def is_truthy(value):
			return str(value or "").strip().lower() in ("1", "yes", "true", "y")

		def is_optional_voucher(voucher):
			return is_truthy(get_child_text(voucher, "ISOPTIONAL"))

		def is_cash_party(party_name):
			return "cash" in str(party_name or "").strip().lower()

		def get_raw_voucher_log_context(voucher, erpnext_doctype=None):
			context = {
				"doctype": erpnext_doctype or "Tally Voucher",
				"tally_voucher_type": voucher.get("VCHTYPE") or get_child_text(voucher, "VOUCHERTYPENAME"),
				"tally_voucher_no": get_child_text(voucher, "VOUCHERNUMBER"),
				"tally_guid": voucher.get("REMOTEID") or voucher.get("GUID") or get_child_text(voucher, "GUID"),
				"posting_date": parse_tally_daybook_date(get_child_text(voucher, "DATE")),
				"party": get_child_text(voucher, "PARTYNAME"),
				"tally_is_optional": 1 if is_optional_voucher(voucher) else 0,
			}

			return {key: value for key, value in context.items() if value}

		def get_vouchers(collection):
			vouchers = []
			for voucher in collection.find_all("VOUCHER"):
				if str(get_child_text(voucher, "ISCANCELLED") or "").strip().lower() == "yes":
					continue

				function, erpnext_doctype = get_voucher_converter(voucher)
				if not function:
					mapping = get_voucher_type_mapping(voucher)
					if mapping and mapping.get("enabled"):
						context = get_raw_voucher_log_context(voucher, erpnext_doctype)
						processing_failures.append(context)
						self.log(
							{"day_book_processing_failure": context},
							context=context,
							message=_("No converter is available for this Day Book voucher mapping."),
						)
					continue

				try:
					processed_voucher = function(voucher, erpnext_doctype)
					if processed_voucher:
						vouchers.append(processed_voucher)
				except Exception:
					context = get_raw_voucher_log_context(voucher, erpnext_doctype)
					processing_failures.append(context)
					self.log({"day_book_processing_failure": context}, context=context)
			return vouchers

		def voucher_to_journal_entry(voucher, erpnext_doctype=None):
			accounts = []
			ledger_entries = voucher.find_all("ALLLEDGERENTRIES.LIST") + voucher.find_all(
				"LEDGERENTRIES.LIST"
			)
			for entry in ledger_entries:
				account = {
					"account": encode_company_abbr(entry.LEDGERNAME.string.strip(), self.erpnext_company),
					"cost_center": get_tally_cost_center_from_allocations(entry),
				}
				if entry.ISPARTYLEDGER.string.strip() == "Yes":
					party_details = get_party(entry.LEDGERNAME.string.strip())
					if party_details:
						party_type, party_account = party_details
						account["party_type"] = party_type
						account["account"] = party_account
						account["party"] = entry.LEDGERNAME.string.strip()
				amount = Decimal(entry.AMOUNT.string.strip())
				if amount > 0:
					account["credit_in_account_currency"] = str(abs(amount))
				else:
					account["debit_in_account_currency"] = str(abs(amount))
				accounts.append(account)

			journal_entry = {
				"doctype": "Journal Entry",
				"tally_guid": voucher.GUID.string.strip(),
				"tally_voucher_no": voucher.VOUCHERNUMBER.string.strip() if voucher.VOUCHERNUMBER else "",
				"tally_is_optional": 1 if is_optional_voucher(voucher) else 0,
				"posting_date": voucher.DATE.string.strip(),
				"company": self.erpnext_company,
				"accounts": accounts,
			}
			return journal_entry

		def is_landed_cost_ledger(ledger_name):
			ledger_name = str(ledger_name or "").strip().lower()

			if not ledger_name:
				return False

			excluded_keywords = [
				"vat",
				"tax",
				"gst",
				"cgst",
				"sgst",
				"igst",
				"tds",
				"tcs",
				"cess",
				"round",
				"discount",
				"rebate",
			]

			if any(keyword in ledger_name for keyword in excluded_keywords):
				return False

			landed_cost_keywords = [
				"freight",
				"transport",
				"shipping",
				"carriage",
				"cartage",
				"clearing",
				"forwarding",
				"custom",
				"customs",
				"duty",
				"insurance",
				"handling",
				"loading",
				"unloading",
				"landing",
				"landed",
				"octroi",
				"port",
				"terminal",
				"demurrage",
			]

			return any(keyword in ledger_name for keyword in landed_cost_keywords)

		def get_purchase_landed_cost_charges(voucher):
			charges = []
			ledger_entries = voucher.find_all("ALLLEDGERENTRIES.LIST") + voucher.find_all("LEDGERENTRIES.LIST")

			for entry in ledger_entries:
				ledger_name = get_child_text(entry, "LEDGERNAME")
				is_party_ledger = str(get_child_text(entry, "ISPARTYLEDGER") or "").strip().lower()
				amount_value = get_child_text(entry, "AMOUNT")

				if is_party_ledger == "yes":
					continue

				if not is_landed_cost_ledger(ledger_name):
					continue

				try:
					amount = abs(Decimal(str(amount_value or "0")))
				except Exception:
					amount = Decimal("0")

				if not amount:
					continue

				charges.append(
					{
						"expense_account": encode_company_abbr(ledger_name, self.erpnext_company),
						"description": ledger_name,
						"amount": str(amount),
					}
				)

			return charges

		def voucher_to_invoice(voucher, erpnext_doctype=None):
			voucher_type = voucher.VOUCHERTYPENAME.string.strip()

			if erpnext_doctype == "Sales Invoice" or voucher_type in ["Sales", "Credit Note"]:
				doctype = "Sales Invoice"
				party_field = "customer"
				account_field = "debit_to"
				account_name = encode_company_abbr(self.tally_debtors_account, self.erpnext_company)
				price_list_field = "selling_price_list"
			elif erpnext_doctype == "Purchase Invoice" or voucher_type in ["Purchase", "Debit Note"]:
				doctype = "Purchase Invoice"
				party_field = "supplier"
				account_field = "credit_to"
				account_name = encode_company_abbr(self.tally_creditors_account, self.erpnext_company)
				price_list_field = "buying_price_list"
			else:
				return

			party_name = voucher.PARTYNAME.string.strip() if voucher.PARTYNAME else ""
			if doctype == "Sales Invoice" and is_cash_party(party_name):
				party_name = "Cash Sales"

			invoice = {
				"doctype": doctype,
				party_field: party_name,
				"tally_guid": voucher.GUID.string.strip(),
				"tally_voucher_no": voucher.VOUCHERNUMBER.string.strip() if voucher.VOUCHERNUMBER else "",
				"tally_is_optional": 1 if is_optional_voucher(voucher) else 0,
				"posting_date": voucher.DATE.string.strip(),
				"due_date": voucher.DATE.string.strip(),
				"items": get_voucher_items(voucher, doctype),
				"taxes": get_voucher_taxes(voucher),
				account_field: account_name,
				price_list_field: "Tally Price List",
				"set_posting_time": 1,
				"disable_rounded_total": 1,
				"company": self.erpnext_company,
			}

			if doctype == "Purchase Invoice":
				landed_cost_charges = get_purchase_landed_cost_charges(voucher)
				if landed_cost_charges:
					invoice["_tally_landed_cost_charges"] = landed_cost_charges

			return invoice

		def voucher_to_inventory_document(voucher, erpnext_doctype=None):
			doctype = erpnext_doctype
			items = get_voucher_items(voucher, doctype)

			if not items:
				return

			voucher_date = parse_tally_daybook_date(voucher.DATE.string.strip())

			document = {
				"doctype": doctype,
				"tally_guid": voucher.GUID.string.strip(),
				"tally_voucher_no": voucher.VOUCHERNUMBER.string.strip() if voucher.VOUCHERNUMBER else "",
				"tally_is_optional": 1 if is_optional_voucher(voucher) else 0,
				"company": self.erpnext_company,
				"items": items,
			}

			party_name = voucher.PARTYNAME.string.strip() if voucher.PARTYNAME else ""

			if doctype in ["Delivery Note", "Purchase Receipt", "Stock Entry"]:
				document["posting_date"] = voucher_date
				document["set_posting_time"] = 1

			if doctype in ["Sales Order", "Purchase Order", "Quotation"]:
				document["transaction_date"] = voucher_date

			if doctype == "Sales Order":
				document["delivery_date"] = voucher_date

			if doctype in ["Delivery Note", "Sales Order"]:
				document["customer"] = party_name
			elif doctype in ["Purchase Receipt", "Purchase Order"]:
				document["supplier"] = party_name
			elif doctype == "Quotation":
				document["quotation_to"] = "Customer"
				document["party_name"] = party_name
			elif doctype == "Stock Entry":
				mapping = get_voucher_type_mapping(voucher)
				document["stock_entry_type"] = (
					mapping.get("erpnext_voucher_type")
					if mapping and mapping.get("erpnext_voucher_type")
					else "Material Transfer"
				)

			return document

		def get_voucher_items(voucher, doctype):
			inventory_entries = get_inventory_entries(voucher)

			def get_entry_text(entry, fieldname, default=""):
				child = entry.find(fieldname)
				return child.get_text(strip=True) if child else default

			def parse_qty_and_uom(value):
				parts = str(value or "").strip().split()
				if not parts:
					return "0", ""

				qty = parts[0].strip()
				uom = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
				return qty, uom

			def get_entry_rate(entry):
				rate = get_entry_text(entry, "RATE")
				if not rate:
					return "0"

				return rate.split("/")[0].strip()

			def get_entry_account(entry):
				allocations = entry.find_all("ACCOUNTINGALLOCATIONS.LIST")
				if not allocations:
					return ""

				ledger_name = get_entry_text(allocations[0], "LEDGERNAME")
				if not ledger_name:
					return ""

				return encode_company_abbr(ledger_name, self.erpnext_company)

			def get_entry_cost_center(entry):
				allocations = entry.find_all("ACCOUNTINGALLOCATIONS.LIST")
				for allocation in allocations:
					for category_allocation in allocation.find_all("CATEGORYALLOCATIONS.LIST"):
						for cost_center_allocation in category_allocation.find_all("COSTCENTREALLOCATIONS.LIST"):
							cost_center_name = get_entry_text(cost_center_allocation, "NAME")
							if cost_center_name:
								return encode_company_abbr(cost_center_name, self.erpnext_company)

				return self.default_cost_center

			def should_set_income_or_expense_account(target_doctype):
				return target_doctype in ["Sales Invoice", "Purchase Invoice"]

			items = []

			for entry in inventory_entries:
				actual_qty, uom = parse_qty_and_uom(get_entry_text(entry, "ACTUALQTY"))
				billed_qty, billed_uom = parse_qty_and_uom(get_entry_text(entry, "BILLEDQTY"))
				qty = billed_qty if billed_qty and billed_qty != "0" else actual_qty
				uom = billed_uom or uom

				item = {
					"item_code": get_entry_text(entry, "STOCKITEMNAME"),
					"description": get_entry_text(entry, "STOCKITEMNAME"),
					"qty": str(abs(Decimal(qty or "0"))),
					"uom": uom,
					"conversion_factor": 1,
					"price_list_rate": get_entry_rate(entry),
					"cost_center": get_entry_cost_center(entry),
				}

				if doctype in [
					"Sales Invoice",
					"Purchase Invoice",
					"Delivery Note",
					"Purchase Receipt",
					"Sales Order",
					"Purchase Order",
					"Quotation",
				]:
					item["warehouse"] = self.default_warehouse

				if doctype in ["Sales Invoice", "Delivery Note", "Sales Order", "Quotation"]:
					item["rate"] = item["price_list_rate"]

				if doctype == "Sales Order":
					item["delivery_date"] = parse_tally_daybook_date(voucher.DATE.string.strip())

				if doctype in ["Purchase Invoice", "Purchase Receipt", "Purchase Order"]:
					item["rate"] = item["price_list_rate"]

				if doctype == "Purchase Order":
					item["schedule_date"] = parse_tally_daybook_date(voucher.DATE.string.strip())

				if should_set_income_or_expense_account(doctype):
					account = get_entry_account(entry)
					if account:
						if doctype == "Sales Invoice":
							item["income_account"] = account
						elif doctype == "Purchase Invoice":
							item["expense_account"] = account

				if doctype == "Stock Entry":
					qty_decimal = Decimal(actual_qty or "0")
					entry_name = str(entry.name or "").upper()

					item.pop("warehouse", None)
					item.pop("price_list_rate", None)
					item.pop("rate", None)
					item["qty"] = str(abs(qty_decimal))

					if "OUT" in entry_name or qty_decimal < 0:
						item["s_warehouse"] = self.default_warehouse
					else:
						item["t_warehouse"] = self.default_warehouse

				if item.get("item_code") and Decimal(item.get("qty") or "0") != 0:
					items.append(item)

			return items

		def get_voucher_taxes(voucher):
			ledger_entries = voucher.find_all("ALLLEDGERENTRIES.LIST") + voucher.find_all(
				"LEDGERENTRIES.LIST"
			)
			taxes = []
			for entry in ledger_entries:
				if entry.ISPARTYLEDGER.string.strip() == "No":
					tax_account = encode_company_abbr(entry.LEDGERNAME.string.strip(), self.erpnext_company)
					taxes.append(
						{
							"charge_type": "Actual",
							"account_head": tax_account,
							"description": tax_account,
							"tax_amount": entry.AMOUNT.string.strip(),
							"cost_center": get_tally_cost_center_from_allocations(entry),
						}
					)
			return taxes

		def get_party(party):
			if frappe.db.exists({"doctype": "Supplier", "supplier_name": party}):
				return "Supplier", encode_company_abbr(self.tally_creditors_account, self.erpnext_company)
			elif frappe.db.exists({"doctype": "Customer", "customer_name": party}):
				return "Customer", encode_company_abbr(self.tally_debtors_account, self.erpnext_company)


		def parse_tally_daybook_date(value):
			value = str(value or "").strip()

			if len(value) == 8 and value.isdigit():
				return value[:4] + "-" + value[4:6] + "-" + value[6:8]

			return value or None

		def get_child_text(tag, child_name):
			child = tag.find(child_name)
			return child.get_text(strip=True) if child else None

		def normalize_daybook_vouchers(collection, processed_vouchers):
			voucher_meta_by_guid = {}
			voucher_meta_by_number = {}

			for voucher in collection.find_all("VOUCHER"):
				guid = voucher.get("REMOTEID") or voucher.get("GUID") or get_child_text(voucher, "GUID")
				voucher_number = get_child_text(voucher, "VOUCHERNUMBER")
				voucher_type = voucher.get("VCHTYPE") or get_child_text(voucher, "VOUCHERTYPENAME")
				persisted_view = voucher.get("OBJVIEW") or get_child_text(voucher, "PERSISTEDVIEW")
				voucher_date = get_child_text(voucher, "DATE")
				is_optional = is_optional_voucher(voucher)

				meta = {
					"tally_voucher_type": voucher_type,
					"tally_persisted_view": persisted_view,
					"tally_voucher_number": voucher_number,
					"tally_date": voucher_date,
					"tally_is_optional": 1 if is_optional else 0,
					"posting_date": parse_tally_daybook_date(voucher_date),
				}

				if guid:
					voucher_meta_by_guid[guid] = meta

				if voucher_number:
					voucher_meta_by_number.setdefault(voucher_number, meta)

			for processed in processed_vouchers:
				guid = processed.get("tally_guid")
				voucher_number = processed.get("tally_voucher_no") or processed.get("tally_voucher_number")

				meta = voucher_meta_by_guid.get(guid) or voucher_meta_by_number.get(voucher_number) or {}

				if meta.get("tally_voucher_type"):
					processed["tally_voucher_type"] = meta.get("tally_voucher_type")

				if meta.get("tally_persisted_view"):
					processed["tally_persisted_view"] = meta.get("tally_persisted_view")

				if meta.get("tally_voucher_number"):
					processed["tally_voucher_number"] = meta.get("tally_voucher_number")

				if meta.get("tally_date"):
					processed["tally_date"] = meta.get("tally_date")

				processed["tally_is_optional"] = 1 if meta.get("tally_is_optional") else 0

				if meta.get("posting_date"):
					processed["posting_date"] = meta.get("posting_date")
				else:
					processed["posting_date"] = parse_tally_daybook_date(processed.get("posting_date"))

				if processed.get("due_date"):
					processed["due_date"] = parse_tally_daybook_date(processed.get("due_date"))

			return processed_vouchers
		try:
			self.publish("Process Day Book Data", _("Reading Uploaded File"), 1, 3)
			collection = self.get_collection(self.day_book_data)

			self.publish("Process Day Book Data", _("Processing Vouchers"), 2, 3)
			self.upsert_voucher_type_mappings_from_daybook(collection)
			vouchers = get_vouchers(collection)
			if processing_failures:
				failure_message = _("Day Book processing failed for {0} voucher(s).").format(
					len(processing_failures)
				)
				self.publish("Process Day Book Data", _("Process Failed"), -1, 3)
				self.log(
					{
						"error": "Day Book processing failed for one or more vouchers.",
						"failed_vouchers": processing_failures[:50],
						"total_failures": len(processing_failures),
					},
					message=failure_message,
				)
				if raise_on_failure:
					frappe.throw(failure_message)
				return

			vouchers = normalize_daybook_vouchers(collection, vouchers)

			self.publish("Process Day Book Data", _("Done"), 3, 3)
			self.dump_processed_data({"vouchers": vouchers})

			self.is_day_book_data_processed = 1
			return vouchers

		except Exception:
			self.publish("Process Day Book Data", _("Process Failed"), -1, 5)
			self.log()
			if raise_on_failure:
				raise

		finally:
			self.set_status()

	def _load_day_book_vouchers(self):
		if not self.vouchers:
			frappe.throw(_("Please process Day Book Data before importing."))

		vouchers_file = frappe.get_doc("File", {"file_url": self.vouchers})
		vouchers = json.loads(vouchers_file.get_content())

		if not isinstance(vouchers, list):
			frappe.throw(_("Processed Day Book voucher file is invalid. Please process Day Book Data again."))

		return vouchers

	def _parse_import_date(self, value):
		value = str(value or "").strip()
		if not value:
			return None

		if len(value) == 8 and value.isdigit():
			value = value[:4] + "-" + value[4:6] + "-" + value[6:8]

		return getdate(value)

	def _get_voucher_import_context(self, voucher):
		if isinstance(voucher, Document):
			data = voucher.as_dict()
		elif isinstance(voucher, dict):
			data = voucher
		else:
			data = {}

		party = (
			data.get("party")
			or data.get("customer")
			or data.get("supplier")
			or data.get("party_name")
			or data.get("employee")
		)

		context = {
			"doctype": data.get("doctype"),
			"tally_voucher_type": data.get("tally_voucher_type"),
			"tally_voucher_no": data.get("tally_voucher_no") or data.get("tally_voucher_number"),
			"tally_guid": data.get("tally_guid"),
			"party": party,
			"posting_date": data.get("posting_date") or data.get("transaction_date"),
		}

		return {key: value for key, value in context.items() if value}

	def _format_voucher_context(self, context):
		keys = ("doctype", "tally_voucher_type", "tally_voucher_no", "tally_guid", "party", "posting_date")
		return ", ".join(f"{key}: {context.get(key)}" for key in keys if context.get(key))

	def _get_price_list_currency(self, company):
		return (
			frappe.db.get_value("Company", company, "default_currency")
			or frappe.db.get_single_value("Global Defaults", "default_currency")
			or "SAR"
		)

	def _ensure_tally_price_list(self, company=None):
		price_list_name = "Tally Price List"
		currency = self._get_price_list_currency(company or self.erpnext_company)

		if frappe.db.exists("Price List", price_list_name):
			price_list = frappe.get_doc("Price List", price_list_name)
			price_list.enabled = 1
			price_list.selling = 1
			price_list.buying = 1
			price_list.currency = currency
			price_list.save(ignore_permissions=True)
			return price_list.name

		price_list = frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": price_list_name,
				"selling": 1,
				"buying": 1,
				"enabled": 1,
				"currency": currency,
			}
		)

		try:
			price_list.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			frappe.clear_messages()
			return price_list_name

		return price_list.name

	def _get_stock_received_but_not_billed_parent_account(self, company):
		if not company or not frappe.db.exists("Company", company):
			return None

		abbr = frappe.db.get_value("Company", company, "abbr")
		for candidate in (
			f"Current Liabilities - {abbr}",
			f"Accounts Payable - {abbr}",
			f"Sundry Creditors - {abbr}",
		):
			if frappe.db.exists("Account", candidate) and frappe.db.get_value("Account", candidate, "is_group"):
				return candidate

		return frappe.db.get_value(
			"Account",
			{
				"company": company,
				"root_type": "Liability",
				"is_group": 1,
			},
			"name",
			order_by="lft asc",
		)

	def _ensure_stock_received_but_not_billed_account(self, company):
		if not company or not frappe.db.exists("Company", company):
			return None

		existing = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
		if existing and frappe.db.exists("Account", existing):
			if not frappe.db.get_value("Account", existing, "is_group"):
				return existing
			return None

		abbr = frappe.db.get_value("Company", company, "abbr")
		account_name = f"Stock Received But Not Billed - {abbr}"

		if frappe.db.exists("Account", account_name):
			if frappe.db.get_value("Account", account_name, "is_group"):
				return None
			account = account_name
		else:
			parent_account = self._get_stock_received_but_not_billed_parent_account(company)
			if not parent_account:
				return None

			account_doc = frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "Stock Received But Not Billed",
					"company": company,
					"parent_account": parent_account,
					"root_type": "Liability",
					"report_type": "Balance Sheet",
					"account_type": "Stock Received But Not Billed",
					"is_group": 0,
				}
			)

			try:
				account_doc.insert(ignore_permissions=True)
				account = account_doc.name
			except frappe.DuplicateEntryError:
				frappe.clear_messages()
				account = account_name

		frappe.db.set_value("Company", company, "stock_received_but_not_billed", account)
		return account

	def _is_truthy(self, value):
		return str(value or "").strip().lower() in ("1", "yes", "true", "y")

	def _ensure_leaf_customer_group(self, group_name="Tally Customers"):
		group_name = group_name or "Tally Customers"

		if frappe.db.exists("Customer Group", group_name):
			if not frappe.db.get_value("Customer Group", group_name, "is_group"):
				return group_name

			leaf_group = f"{group_name} Leaf"
			if frappe.db.exists("Customer Group", leaf_group):
				return leaf_group

			customer_group = frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": leaf_group,
					"parent_customer_group": group_name,
					"is_group": 0,
				}
			)
			customer_group.insert(ignore_permissions=True)
			return customer_group.name

		customer_group = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": group_name,
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}
		)
		customer_group.insert(ignore_permissions=True)
		return customer_group.name

	def _ensure_cash_sales_customer(self):
		customer_name = "Cash Sales"
		existing = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
		if existing:
			return existing

		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": customer_name,
				"customer_group": self._ensure_leaf_customer_group(),
				"customer_type": "Individual",
			}
		)

		try:
			customer.insert(ignore_permissions=True)
			return customer.name
		except frappe.DuplicateEntryError:
			frappe.clear_messages()
			return frappe.db.get_value("Customer", {"customer_name": customer_name}, "name") or customer_name

	def _get_round_off_parent_account(self, company):
		if not company or not frappe.db.exists("Company", company):
			return None

		candidates = [
			encode_company_abbr("Indirect Expenses", company),
			encode_company_abbr("Indirect Expense", company),
		]
		for candidate in candidates:
			if frappe.db.exists("Account", candidate) and frappe.db.get_value("Account", candidate, "is_group"):
				return candidate

		parent = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"account_name": "Indirect Expenses",
				"is_group": 1,
			},
			"name",
		)
		if parent:
			return parent

		parent = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"account_type": "Indirect Expense",
				"is_group": 1,
			},
			"name",
			order_by="lft asc",
		)
		if parent:
			return parent

		root_expense_group = frappe.db.get_value(
			"Account",
			{
				"company": company,
				"root_type": "Expense",
				"is_group": 1,
			},
			"name",
			order_by="lft asc",
		)
		if not root_expense_group:
			return None

		indirect_expenses = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Indirect Expenses",
				"company": company,
				"parent_account": root_expense_group,
				"root_type": "Expense",
				"report_type": "Profit and Loss",
				"account_type": "Indirect Expense",
				"is_group": 1,
			}
		)

		try:
			indirect_expenses.insert(ignore_permissions=True)
			return indirect_expenses.name
		except frappe.DuplicateEntryError:
			frappe.clear_messages()
			return encode_company_abbr("Indirect Expenses", company)

	def _ensure_round_off_account(self, company=None):
		company = company or self.erpnext_company
		if not company or not frappe.db.exists("Company", company):
			return None

		account_name = encode_company_abbr("Round Off", company)
		if frappe.db.exists("Account", account_name):
			account = account_name
			if frappe.db.get_value("Account", account, "is_group"):
				return None
			if frappe.db.get_value("Account", account, "account_type") != "Round Off":
				frappe.db.set_value("Account", account, "account_type", "Round Off")
		else:
			parent_account = self._get_round_off_parent_account(company)
			if not parent_account:
				return None

			account_doc = frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "Round Off",
					"company": company,
					"parent_account": parent_account,
					"root_type": "Expense",
					"report_type": "Profit and Loss",
					"account_type": "Round Off",
					"is_group": 0,
				}
			)

			try:
				account_doc.insert(ignore_permissions=True)
				account = account_doc.name
			except frappe.DuplicateEntryError:
				frappe.clear_messages()
				account = account_name

		frappe.db.set_value("Company", company, "round_off_account", account)
		self.default_round_off_account = account
		return account

	def _validate_day_book_import_prerequisites(self, vouchers=None, create_missing_defaults=False):
		issues = []

		if not self.is_master_data_imported:
			issues.append(_("Import Master Data before importing Day Book Data."))

		if not self.is_day_book_data_processed:
			issues.append(_("Process Day Book Data before importing."))

		if not self.erpnext_company or not frappe.db.exists("Company", self.erpnext_company):
			issues.append(_("Select a valid ERPNext Company."))

		try:
			vouchers = vouchers if vouchers is not None else self._load_day_book_vouchers()
		except Exception as exc:
			issues.append(_("Load processed Day Book vouchers: {0}").format(exc))
			vouchers = []

		if not vouchers:
			issues.append(_("No processed vouchers found. Process Day Book Data again."))

		def require_link(label, doctype, value, must_be_leaf=False):
			if not value:
				issues.append(_("Set {0}.").format(label))
				return

			if not frappe.db.exists(doctype, value):
				issues.append(_("Create or select {0}: {1}.").format(label, value))
				return

			if must_be_leaf and frappe.db.get_value(doctype, value, "is_group"):
				issues.append(_("{0} must be a non-group {1}: {2}.").format(label, doctype, value))

		if self.erpnext_company:
			if self.tally_creditors_account:
				require_link(
					_("Tally Creditors Account in ERPNext"),
					"Account",
					encode_company_abbr(self.tally_creditors_account, self.erpnext_company),
					must_be_leaf=True,
				)
			else:
				issues.append(_("Set Tally Creditors Account."))

			if self.tally_debtors_account:
				require_link(
					_("Tally Debtors Account in ERPNext"),
					"Account",
					encode_company_abbr(self.tally_debtors_account, self.erpnext_company),
					must_be_leaf=True,
				)
			else:
				issues.append(_("Set Tally Debtors Account."))

		require_link(_("Default Cost Center"), "Cost Center", self.default_cost_center, must_be_leaf=True)
		if create_missing_defaults and not self.default_round_off_account:
			self._ensure_round_off_account(self.erpnext_company)
		require_link(_("Default Round Off Account"), "Account", self.default_round_off_account, must_be_leaf=True)

		warehouse_doctypes = {
			"Sales Invoice",
			"Purchase Invoice",
			"Delivery Note",
			"Purchase Receipt",
			"Sales Order",
			"Purchase Order",
			"Quotation",
			"Stock Entry",
		}
		needs_warehouse = any(voucher.get("doctype") in warehouse_doctypes for voucher in vouchers)
		if needs_warehouse:
			require_link(_("Default Warehouse"), "Warehouse", self.default_warehouse, must_be_leaf=True)

		needs_cash_sales_customer = any(
			voucher.get("doctype") == "Sales Invoice"
			and (
				voucher.get("customer") == "Cash Sales"
				or "cash" in str(voucher.get("customer") or voucher.get("party") or "").strip().lower()
			)
			for voucher in vouchers
		)
		if needs_cash_sales_customer:
			if create_missing_defaults:
				try:
					self._ensure_cash_sales_customer()
				except Exception as exc:
					issues.append(_("Create Cash Sales customer: {0}").format(exc))
			elif not frappe.db.get_value("Customer", {"customer_name": "Cash Sales"}, "name"):
				issues.append(_("Create Customer Cash Sales for cash-party Sales Invoices."))

		invalid_dates = []
		for row_number, voucher in enumerate(vouchers, start=1):
			try:
				if not self._parse_import_date(voucher.get("posting_date") or voucher.get("transaction_date")):
					raise ValueError("Missing posting date")
			except Exception:
				context = self._get_voucher_import_context(voucher)
				context["row"] = row_number
				invalid_dates.append(context)

		if invalid_dates:
			examples = "; ".join(self._format_voucher_context(context) for context in invalid_dates[:5])
			issues.append(_("Set valid posting dates on processed vouchers. Examples: {0}").format(examples))

		purchase_invoice_companies = {
			voucher.get("company") or self.erpnext_company
			for voucher in vouchers
			if voucher.get("doctype") == "Purchase Invoice"
		}
		for company in sorted(company for company in purchase_invoice_companies if company):
			currency = self._get_price_list_currency(company)
			if not currency:
				issues.append(_("Set a default currency for company {0} or in Global Defaults.").format(company))
			elif not frappe.db.exists("Currency", currency):
				issues.append(_("Create Currency {0} before importing Purchase Invoices.").format(currency))

			existing_srnb = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
			if existing_srnb and frappe.db.exists("Account", existing_srnb):
				if frappe.db.get_value("Account", existing_srnb, "is_group"):
					issues.append(
						_("Company {0} Stock Received But Not Billed account must be non-group: {1}.").format(
							company, existing_srnb
						)
					)
				continue

			if create_missing_defaults:
				try:
					self._ensure_tally_price_list(company)
					if not self._ensure_stock_received_but_not_billed_account(company):
						issues.append(
							_(
								"Create a Liability group account for company {0}, or set Company Stock Received But Not Billed to a non-group Liability account."
							).format(company)
						)
				except Exception as exc:
					issues.append(_("Prepare Purchase Invoice defaults for company {0}: {1}").format(company, exc))
			elif not self._get_stock_received_but_not_billed_parent_account(company):
				issues.append(
					_(
						"Create a Liability group account for company {0}, or set Company Stock Received But Not Billed to a non-group Liability account."
					).format(company)
				)

		if issues:
			message = _("Resolve these actions before importing Day Book Data:") + "<ul>"
			message += "".join("<li>{0}</li>".format(issue) for issue in issues)
			message += "</ul>"
			frappe.throw(message, title=_("Day Book Import Validation Failed"))

		return vouchers

	def _get_fiscal_year_rows(self):
		fiscal_years = frappe.get_all(
			"Fiscal Year",
			fields=["name", "year_start_date", "year_end_date", "disabled", "is_short_year"],
			order_by="year_start_date asc",
		)

		for fiscal_year in fiscal_years:
			fiscal_year.year_start_date = getdate(fiscal_year.year_start_date)
			fiscal_year.year_end_date = getdate(fiscal_year.year_end_date)

		return fiscal_years

	def _find_fiscal_year_for_date(self, fiscal_years, posting_date):
		for fiscal_year in fiscal_years:
			if fiscal_year.year_start_date <= posting_date <= fiscal_year.year_end_date:
				return fiscal_year

		return None

	def _get_fiscal_year_name(self, start_date, end_date):
		start_date = getdate(start_date)
		end_date = getdate(end_date)

		if start_date.year == end_date.year:
			return str(start_date.year)

		return f"{start_date.year}-{end_date.year}"

	def _insert_fiscal_year(self, start_date, end_date, template=None):
		start_date = getdate(start_date)
		end_date = getdate(end_date)
		fiscal_year_name = self._get_fiscal_year_name(start_date, end_date)

		if frappe.db.exists("Fiscal Year", fiscal_year_name):
			fiscal_year = frappe.get_doc("Fiscal Year", fiscal_year_name)
			if fiscal_year.disabled:
				fiscal_year.disabled = 0
				fiscal_year.save(ignore_permissions=True)
			return fiscal_year

		fiscal_year = frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": fiscal_year_name,
				"year_start_date": start_date,
				"year_end_date": end_date,
				"disabled": 0,
				"is_short_year": 0,
				"auto_created": 1,
			}
		)

		if template and template.get("name"):
			template_doc = frappe.get_doc("Fiscal Year", template.name)
			for row in template_doc.get("companies"):
				fiscal_year.append("companies", {"company": row.company})

		try:
			fiscal_year.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			frappe.clear_messages()
			fiscal_year = frappe.get_doc("Fiscal Year", fiscal_year_name)

		return fiscal_year

	def _create_required_fiscal_years(self, vouchers):
		posting_dates = sorted(
			{
				self._parse_import_date(voucher.get("posting_date") or voucher.get("transaction_date"))
				for voucher in vouchers
				if voucher.get("posting_date") or voucher.get("transaction_date")
			}
		)

		for posting_date in posting_dates:
			fiscal_years = self._get_fiscal_year_rows()
			covering_fiscal_year = self._find_fiscal_year_for_date(fiscal_years, posting_date)

			if covering_fiscal_year:
				if covering_fiscal_year.disabled:
					frappe.db.set_value("Fiscal Year", covering_fiscal_year.name, "disabled", 0)
				continue

			regular_fiscal_years = [
				fiscal_year for fiscal_year in fiscal_years if not fiscal_year.get("is_short_year")
			]
			if not regular_fiscal_years:
				start_date = getdate(f"{posting_date.year}-01-01")
				end_date = getdate(f"{posting_date.year}-12-31")
				self._insert_fiscal_year(start_date, end_date)
				continue

			previous_fiscal_year = None
			next_fiscal_year = None
			for fiscal_year in regular_fiscal_years:
				if fiscal_year.year_end_date < posting_date:
					previous_fiscal_year = fiscal_year
				elif fiscal_year.year_start_date > posting_date and not next_fiscal_year:
					next_fiscal_year = fiscal_year

			if previous_fiscal_year:
				start_date = add_days(previous_fiscal_year.year_end_date, 1)
				end_date = add_years(previous_fiscal_year.year_end_date, 1)
				template = previous_fiscal_year

				if next_fiscal_year and getdate(end_date) >= next_fiscal_year.year_start_date:
					start_date = add_years(next_fiscal_year.year_start_date, -1)
					end_date = add_years(next_fiscal_year.year_end_date, -1)
					template = next_fiscal_year
			elif next_fiscal_year:
				start_date = add_years(next_fiscal_year.year_start_date, -1)
				end_date = add_years(next_fiscal_year.year_end_date, -1)
				template = next_fiscal_year
			else:
				start_date = getdate(f"{posting_date.year}-01-01")
				end_date = getdate(f"{posting_date.year}-12-31")
				template = None

			while posting_date < getdate(start_date):
				inserted = self._insert_fiscal_year(start_date, end_date, template)
				template = frappe._dict(
					{
						"name": inserted.name,
						"year_start_date": getdate(inserted.year_start_date),
						"year_end_date": getdate(inserted.year_end_date),
						"is_short_year": inserted.is_short_year,
					}
				)
				start_date = add_years(template.year_start_date, -1)
				end_date = add_years(template.year_end_date, -1)

			while posting_date > getdate(end_date):
				inserted = self._insert_fiscal_year(start_date, end_date, template)
				template = frappe._dict(
					{
						"name": inserted.name,
						"year_start_date": getdate(inserted.year_start_date),
						"year_end_date": getdate(inserted.year_end_date),
						"is_short_year": inserted.is_short_year,
					}
				)
				start_date = add_days(template.year_end_date, 1)
				end_date = add_years(template.year_end_date, 1)

			self._insert_fiscal_year(start_date, end_date, template)

	def _import_day_book_data(self):
		def create_custom_fields():
			_create_custom_fields(
				{
					(
						"Journal Entry",
						"Purchase Invoice",
						"Sales Invoice",
						"Delivery Note",
						"Purchase Receipt",
						"Sales Order",
						"Purchase Order",
						"Quotation",
						"Stock Entry",
						"Landed Cost Voucher",
					): [
						{
							"fieldtype": "Data",
							"fieldname": "tally_guid",
							"read_only": 1,
							"label": "Tally GUID",
						},
						{
							"fieldtype": "Data",
							"fieldname": "tally_voucher_no",
							"read_only": 1,
							"label": "Tally Voucher Number",
						},
						{
							"fieldtype": "Data",
							"fieldname": "tally_voucher_type",
							"read_only": 1,
							"label": "Tally Voucher Type",
						},
						{
							"fieldtype": "Data",
							"fieldname": "tally_persisted_view",
							"read_only": 1,
							"label": "Tally Persisted View",
						},
						{
							"fieldtype": "Data",
							"fieldname": "tally_voucher_number",
							"read_only": 1,
							"label": "Tally Voucher Number Raw",
						},
						{
							"fieldtype": "Data",
							"fieldname": "tally_date",
							"read_only": 1,
							"label": "Tally Date Raw",
						},
						{
							"fieldtype": "Check",
							"fieldname": "tally_is_optional",
							"read_only": 1,
							"label": "Tally Optional Voucher",
						},
					]
				}
			)

		try:
			vouchers = self._process_day_book_data(raise_on_failure=True)
			self.set_status("Importing Day Book Data")
			if not vouchers:
				vouchers = self._load_day_book_vouchers()
			self._validate_day_book_import_prerequisites(vouchers, create_missing_defaults=True)

			frappe.db.set_value(
				"Account",
				encode_company_abbr(self.tally_creditors_account, self.erpnext_company),
				"account_type",
				"Payable",
			)
			frappe.db.set_value(
				"Account",
				encode_company_abbr(self.tally_debtors_account, self.erpnext_company),
				"account_type",
				"Receivable",
			)
			frappe.db.set_value(
				"Company", self.erpnext_company, "round_off_account", self.default_round_off_account
			)

			self._create_required_fiscal_years(vouchers)
			self._ensure_tally_price_list(self.erpnext_company)
			create_custom_fields()
			frappe.db.commit()  # nosemgrep: persist setup documents before voucher import starts.

			total = len(vouchers)
			failed_vouchers = 0

			for index in range(0, total, VOUCHER_CHUNK_SIZE):
				failed_vouchers += self._import_vouchers(start=index, total=total)

			if failed_vouchers:
				self.is_day_book_data_imported = 0
				self.publish(
					"Importing Vouchers",
					_("{0} of {1} voucher(s) failed. Review the Import Log.").format(failed_vouchers, total),
					-1,
					total or 1,
				)
				self.log(
					{
						"error": "Day Book import completed with voucher failures.",
						"failed_vouchers": failed_vouchers,
						"total_vouchers": total,
					},
					message=_("Day Book import completed with {0} failed voucher(s).").format(failed_vouchers),
				)
			else:
				self.status = ""
				self.is_day_book_data_imported = 1
				self.save()
				frappe.db.commit()  # nosemgrep: mark Day Book import complete only after all chunks pass.

		except Exception:
			self.publish("Importing Vouchers", _("Import Failed"), -1, 1)
			self.log()

		finally:
			self.set_status()

	def _import_vouchers(self, start, total):
		frappe.flags.in_migrate = True
		vouchers_file = frappe.get_doc("File", {"file_url": self.vouchers})
		vouchers = json.loads(vouchers_file.get_content())
		chunk = vouchers[start : start + VOUCHER_CHUNK_SIZE]

		def create_landed_cost_voucher_if_required(voucher_doc, landed_cost_charges, source_voucher):
			if voucher_doc.doctype != "Purchase Invoice":
				return

			if not landed_cost_charges:
				return

			if not frappe.db.exists("DocType", "Landed Cost Voucher"):
				return

			lcv_guid = ""
			if source_voucher.get("tally_guid"):
				lcv_guid = str(source_voucher.get("tally_guid")) + "-LCV"

			if lcv_guid and frappe.db.exists("Landed Cost Voucher", {"tally_guid": lcv_guid}):
				return

			taxes = []
			for charge in landed_cost_charges:
				try:
					amount = abs(Decimal(str(charge.get("amount") or "0")))
				except Exception:
					amount = Decimal("0")

				if not amount:
					continue

				taxes.append(
					{
						"expense_account": charge.get("expense_account"),
						"description": charge.get("description") or charge.get("expense_account"),
						"amount": str(amount),
					}
				)

			if not taxes:
				return

			landed_cost_voucher = frappe.get_doc(
				{
					"doctype": "Landed Cost Voucher",
					"company": self.erpnext_company,
					"posting_date": voucher_doc.posting_date,
					"distribute_charges_based_on": "Amount",
					"purchase_receipts": [
						{
							"receipt_document_type": "Purchase Invoice",
							"receipt_document": voucher_doc.name,
						}
					],
					"taxes": taxes,
					"tally_guid": lcv_guid,
					"tally_voucher_no": source_voucher.get("tally_voucher_no"),
					"tally_voucher_type": source_voucher.get("tally_voucher_type"),
					"tally_persisted_view": source_voucher.get("tally_persisted_view"),
					"tally_voucher_number": source_voucher.get("tally_voucher_number"),
					"tally_date": source_voucher.get("tally_date"),
				}
			)

			landed_cost_voucher.flags.ignore_mandatory = True
			landed_cost_voucher.get_items_from_purchase_receipts()
			landed_cost_voucher.insert(ignore_permissions=True)
			landed_cost_voucher.submit()

		failed_vouchers = 0
		for index, voucher in enumerate(chunk, start=start):
			voucher_doc = None
			landed_cost_charges = voucher.pop("_tally_landed_cost_charges", []) or []
			keep_as_draft = self._is_truthy(voucher.get("tally_is_optional"))

			try:
				if voucher.get("doctype") == "Purchase Invoice":
					company = voucher.get("company") or self.erpnext_company
					voucher["buying_price_list"] = self._ensure_tally_price_list(company)
					if not self._ensure_stock_received_but_not_billed_account(company):
						frappe.throw(
							_(
								"Could not prepare Stock Received But Not Billed account for company {0}."
							).format(company)
						)

				voucher_doc = frappe.get_doc(voucher)
				voucher_doc.insert(ignore_permissions=True)
				if not keep_as_draft:
					voucher_doc.submit()
					create_landed_cost_voucher_if_required(voucher_doc, landed_cost_charges, voucher)
				self.publish("Importing Vouchers", _("{} of {}").format(index, total), index, total)
				frappe.db.commit()  # nosemgrep: keep each queued voucher import durable.
			except Exception:
				failed_vouchers += 1
				frappe.db.rollback()  # nosemgrep: rollback only the failed voucher in the long queue.
				context = self._get_voucher_import_context(voucher_doc or voucher)
				self.log(voucher_doc or voucher, context=context)
				frappe.db.commit()  # nosemgrep: persist the voucher error log before continuing.

		frappe.flags.in_migrate = False
		return failed_vouchers

	@frappe.whitelist()
	def process_master_data(self):
		self.set_status("Processing Master Data")
		frappe.enqueue_doc(self.doctype, self.name, "_process_master_data", queue="long", timeout=3600)

	@frappe.whitelist()
	def import_master_data(self):
		self.set_status("Importing Master Data")
		frappe.enqueue_doc(self.doctype, self.name, "_import_master_data", queue="long", timeout=3600)

	@frappe.whitelist()
	def process_day_book_data(self):
		self.set_status("Processing Day Book Data")
		frappe.enqueue_doc(self.doctype, self.name, "_process_day_book_data", queue="long", timeout=3600)

	@frappe.whitelist()
	def import_day_book_data(self):
		self._validate_day_book_import_prerequisites()
		self.set_status("Importing Day Book Data")
		frappe.enqueue_doc(self.doctype, self.name, "_import_day_book_data", queue="long", timeout=7200)

	def log(self, data=None, context=None, message=None):
		exception = sys.exc_info()[1]
		if isinstance(exception, frappe.DuplicateEntryError):
			return

		traceback_text = traceback.format_exc()
		if traceback_text.strip() == "NoneType: None":
			traceback_text = ""

		context_text = ""
		if context:
			context_text = "\n".join(
				[
					"Voucher Context:",
					json.dumps(context, default=str, indent=4),
				]
			)

		exc = "\n\n".join(text for text in (message, context_text, traceback_text) if text)

		if isinstance(data, Document) or (isinstance(data, dict) and data.get("doctype")):
			failed_import_log = json.loads(self.failed_import_log or "[]")
			doc = data.as_dict() if isinstance(data, Document) else dict(data)
			doc.setdefault("creation", now())
			failed_import_log.append({"doc": doc, "exc": exc})
			self.failed_import_log = json.dumps(failed_import_log, default=str, separators=(",", ":"))
			self.save()
			frappe.db.commit() # nosemgrep
			return

		data = data or self.status
		log_message = "\n".join(
			[
				"Data:",
				json.dumps(data, default=str, indent=4),
				"--" * 50,
				"\nException:",
				exc,
			]
		)
		return frappe.log_error(title="Tally Migration Error", message=log_message)

	def set_status(self, status=""):
		self.status = status
		self.save()
		if status:
			self.publish(status, _("Queued or running"), 1, 100)
